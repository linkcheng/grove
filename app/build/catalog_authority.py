"""External-rooted PostgreSQL catalog authority compiler.

The v7 reader projected a live catalog through a relation/function allowlist.
This module is a separate architecture root: discovery starts from catalog
universes and canonicalizes every included fact before comparing it with a
source-controlled expected artifact.  The expected artifact is never generated
or rewritten by normal verification paths.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

import psycopg

from app.contracts.canonical import canonical_bytes

CATALOG_AUTHORITY_COMPILER_VERSION = "catalog-authority-root-v1"
CATALOG_AUTHORITY_EXPECTED_ARTIFACT = Path(__file__).with_name("ws3_catalog_authority_v1.json")
# This value is intentionally duplicated in source.  The expected artifact is
# external evidence, not a file that can authenticate itself by recomputing its
# own digest.  Regenerating the artifact therefore requires an explicit code
# review change to this anchor as well.
CATALOG_AUTHORITY_EXPECTED_ARTIFACT_SHA256 = "309a313e9d10055acaa68760a5ead3d2cfc10c660a72d6b17a849211ae2ecc8a"
CATALOG_AUTHORITY_EXPECTED_ROOT = "151275568c549b6f7a06536181cbc471cd13f0c9a37374a5bac909170b7bd2f2"

_SECTION_NAMES = (
    "compatibility",
    "extensions",
    "namespaces",
    "database",
    "roles",
    "objects",
    "attributes",
    "constraints",
    "indexes",
    "triggers",
    "rewrites",
    "policies",
    "functions",
    "types",
    "acl",
    "ownership",
    "comments",
    "security_labels",
    "extension_dependencies",
    "casts",
    "operators",
    "opclasses",
    "opfamilies",
    "collations",
    "conversions",
    "transforms",
    "text_search",
    "parameter_acls",
    "default_acls",
    "db_role_settings",
    "event_triggers",
    "languages",
    "tablespaces",
    "foreign_data",
    "large_objects",
    "publications",
    "subscriptions",
)

_LITERAL_DEFAULT_RE = re.compile(
    r"^\s*(?:"
    r"NULL|TRUE|FALSE|"
    r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|"
    r"(?:E)?'(?:''|[^'])*'"
    r")(?:\s*::\s*[A-Za-z_][A-Za-z0-9_$.]*(?:\s*\(\s*[^)]*\s*\))?)*\s*$",
    re.IGNORECASE,
)

# Trust/bypass matrix for fields intentionally absent from canonical roots.
# Every omission has a stable semantic replacement in an included field.
CATALOG_AUTHORITY_EXCLUDED_FIELDS: dict[str, str] = {
    "pg_class.oid/relfilenode/relpages/reltuples/relfrozenxid/relminmxid": (
        "physical identity/vacuum/statistics state; relation schema identity, owner, "
        "persistence, partition and access semantics are included"
    ),
    "pg_attribute.attcacheoff": (
        "backend cache offset; complete type, typmod, collation, nullability, default, "
        "identity/generated and storage facts are included"
    ),
    "pg_index.oid/indrelid/indexrelid/indcollation-indclass OIDs": (
        "physical catalog references; relation/index identities, canonical index definition "
        "and semantic flags are included"
    ),
    "pg_depend.objid/refobjid OIDs": (
        "physical references; pg_identify_object identities and extension member semantic hashes are included"
    ),
    "pg_type.typdefaultbin/pg_attrdef.adbin/pg_constraint.conbin node trees": (
        "OID-bearing PostgreSQL internal AST text; canonical pg_get_* renderings and "
        "typed literal/default policy are included"
    ),
    "raw pg_attribute.attmissingval evaluated value for non-literal defaults": (
        "data-plane physical value; atthasmissing, data type and canonical default expression remain included"
    ),
    "pg_database.datfrozenxid/datminmxid": (
        "vacuum horizon rather than authority semantics; database identity, owner, encoding, "
        "locale, connection and ACL facts are included"
    ),
    "pg_subscription_rel.srsublsn": (
        "replication progress is runtime state rather than capability semantics; subscription "
        "relation identity and sync state remain included"
    ),
}


class CatalogAuthorityError(RuntimeError):
    """Raised when a catalog root or external artifact is malformed or mismatched."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_section_root(value: object) -> str:
    """Hash one section using the repository's only canonical byte serializer."""

    return _sha256(canonical_bytes(value))


def _canonical_value(value: Any) -> Any:
    """Convert psycopg container values without coercing semantic scalars."""

    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, bytes):
        return value.hex()
    return value


def _rows(cursor: psycopg.Cursor[Any], statement: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
    """Fetch rows with catalog-provided column names and no expected-name filter."""

    cursor.execute(statement, params)
    description = cursor.description
    if description is None:
        raise CatalogAuthorityError("catalog query returned no column description")
    names = [str(column.name) for column in description]
    result: list[dict[str, Any]] = []
    for row in cursor.fetchall():
        result.append({name: _canonical_value(value) for name, value in zip(names, row, strict=True)})
    return result


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=canonical_bytes)


def _section(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = _sort_rows(rows)
    canonical_rows = [canonical_bytes(row) for row in ordered]
    if len(set(canonical_rows)) != len(canonical_rows):
        raise CatalogAuthorityError("duplicate catalog fact in canonical section")
    return {"count": len(ordered), "root": canonical_section_root(ordered)}


def _acl_entries(cursor: psycopg.Cursor[Any], statement: str) -> list[dict[str, Any]]:
    return _rows(cursor, statement)


_COMPATIBILITY_SQL = """
    SELECT current_database() AS database_name,
           current_setting('server_version_num') AS server_version_num,
           current_setting('server_version') AS server_version,
           current_setting('server_encoding') AS server_encoding
"""

_EXTENSIONS_SQL = """
    SELECT e.extname, e.extversion, owner.rolname AS owner,
           n.nspname AS schema_name, e.extrelocatable,
           COALESCE((
               SELECT jsonb_agg(config_oid::regclass::text ORDER BY config_oid::regclass::text)
                 FROM unnest(COALESCE(e.extconfig, ARRAY[]::oid[])) AS config_oid
           ), '[]'::jsonb) AS config_relations,
           COALESCE(e.extcondition, ARRAY[]::text[]) AS config_conditions
      FROM pg_extension AS e
      JOIN pg_roles AS owner ON owner.oid = e.extowner
      JOIN pg_namespace AS n ON n.oid = e.extnamespace
     ORDER BY e.extname
"""

_EXTENSION_MEMBERS_SQL = """
    SELECT extension.extname,
           dependency.classid::regclass::text AS member_class,
           dependency.objid AS member_oid,
           identified.object_type,
           identified.object_schema,
           identified.object_name,
           identified.object_identity,
           CASE
             WHEN dependency.classid = 'pg_proc'::regclass AND function_row.prokind IN ('f', 'p')
               THEN pg_get_functiondef(dependency.objid)
             WHEN dependency.classid = 'pg_class'::regclass AND relation.relkind = 'i'
               THEN pg_get_indexdef(dependency.objid)
             WHEN dependency.classid = 'pg_class'::regclass AND relation.relkind IN ('v', 'm')
               THEN pg_get_viewdef(dependency.objid, true)
             WHEN dependency.classid = 'pg_constraint'::regclass
               THEN pg_get_constraintdef(dependency.objid, true)
             WHEN dependency.classid = 'pg_trigger'::regclass
               THEN pg_get_triggerdef(dependency.objid, true)
             ELSE identified.object_identity
           END AS semantic_definition
      FROM pg_depend AS dependency
      JOIN pg_extension AS extension ON extension.oid = dependency.refobjid
      LEFT JOIN pg_class AS relation
        ON dependency.classid = 'pg_class'::regclass AND relation.oid = dependency.objid
      LEFT JOIN pg_proc AS function_row
        ON dependency.classid = 'pg_proc'::regclass AND function_row.oid = dependency.objid
      CROSS JOIN LATERAL pg_identify_object(
          dependency.classid, dependency.objid, dependency.objsubid
      ) AS identified(object_type, object_schema, object_name, object_identity)
     WHERE dependency.deptype = 'e'
     ORDER BY extension.extname, identified.object_type, identified.object_identity
"""

_NAMESPACES_SQL = """
    SELECT n.nspname AS schema_name, owner.rolname AS owner,
           COALESCE((
               SELECT jsonb_agg(jsonb_build_object(
                   'grantor', grantor.rolname,
                   'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
                   'privilege', acl_item.privilege_type,
                   'grantable', acl_item.is_grantable
               ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'),
                        acl_item.privilege_type, acl_item.is_grantable)
                 FROM aclexplode(COALESCE(n.nspacl, acldefault('n', n.nspowner))) AS acl_item
                 JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
                 LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee
           ), '[]'::jsonb) AS acl,
           obj_description(n.oid, 'pg_namespace') AS comment
      FROM pg_namespace AS n
      JOIN pg_roles AS owner ON owner.oid = n.nspowner
     ORDER BY n.nspname
"""

_DATABASE_SQL = """
    SELECT d.datname AS database_name, owner.rolname AS owner,
           pg_encoding_to_char(d.encoding) AS encoding, d.datcollate, d.datctype,
           d.datistemplate, d.datallowconn, d.datconnlimit,
           COALESCE((
               SELECT jsonb_agg(jsonb_build_object(
                   'grantor', grantor.rolname,
                   'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
                   'privilege', acl_item.privilege_type,
                   'grantable', acl_item.is_grantable
               ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'),
                        acl_item.privilege_type, acl_item.is_grantable)
                 FROM aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) AS acl_item
                 JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
                 LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee
           ), '[]'::jsonb) AS acl,
           shobj_description(d.oid, 'pg_database') AS comment
      FROM pg_database AS d
      JOIN pg_roles AS owner ON owner.oid = d.datdba
     ORDER BY d.datname
"""

_ROLES_SQL = """
    SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin,
           rolreplication, rolbypassrls, rolconnlimit,
           CASE WHEN rolvaliduntil IS NULL THEN NULL ELSE rolvaliduntil::text END AS rolvaliduntil,
           COALESCE(rolconfig, ARRAY[]::text[]) AS rolconfig
      FROM pg_roles
     WHERE rolname !~ '^pg_'
     ORDER BY rolname
"""

_MEMBERSHIPS_SQL = """
    SELECT member.rolname AS member, granted.rolname AS granted_role,
           grantor.rolname AS grantor, memberships.admin_option,
           memberships.inherit_option, memberships.set_option
      FROM pg_auth_members AS memberships
      JOIN pg_roles AS member ON member.oid = memberships.member
      JOIN pg_roles AS granted ON granted.oid = memberships.roleid
      JOIN pg_roles AS grantor ON grantor.oid = memberships.grantor
     WHERE member.rolname !~ '^pg_' OR granted.rolname !~ '^pg_'
     ORDER BY member.rolname, granted.rolname, grantor.rolname
"""

_OBJECTS_SQL = """
    SELECT n.nspname AS schema_name, c.relname AS object_name, c.relkind,
           c.relpersistence, owner.rolname AS owner, c.relreplident,
           c.relrowsecurity, c.relforcerowsecurity, am.amname AS access_method,
           c.relispopulated,
           COALESCE(ts.spcname, '') AS tablespace,
           COALESCE(c.reloptions, ARRAY[]::text[]) AS reloptions,
           c.relispartition,
           parent_namespace.nspname AS parent_schema, parent.relname AS parent_name,
           CASE WHEN c.relkind = 'S' THEN (
               SELECT jsonb_build_object(
                   'data_type', sequence.seqtypid::regtype::text,
                   'start', sequence.seqstart,
                   'increment', sequence.seqincrement,
                   'max', sequence.seqmax,
                   'min', sequence.seqmin,
                   'cache', sequence.seqcache,
                   'cycle', sequence.seqcycle
               )
                 FROM pg_sequence AS sequence
                WHERE sequence.seqrelid = c.oid
           ) END AS sequence_parameters,
           COALESCE((
               SELECT jsonb_agg(jsonb_build_object(
                   'schema', child_namespace.nspname, 'name', child.relname
               ) ORDER BY child_namespace.nspname, child.relname)
                 FROM pg_inherits AS child_edge
                 JOIN pg_class AS child ON child.oid = child_edge.inhrelid
                 JOIN pg_namespace AS child_namespace ON child_namespace.oid = child.relnamespace
                WHERE child_edge.inhparent = c.oid
           ), '[]'::jsonb) AS children,
           CASE WHEN c.relispartition THEN pg_get_expr(c.relpartbound, c.oid, true) END AS partition_bound,
           CASE WHEN c.relkind = 'p' THEN pg_get_partkeydef(c.oid) END AS partition_key,
           COALESCE((
               SELECT jsonb_agg(jsonb_build_object(
                   'grantor', grantor.rolname,
                   'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
                   'privilege', acl_item.privilege_type,
                   'grantable', acl_item.is_grantable
               ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'),
                        acl_item.privilege_type, acl_item.is_grantable)
                 FROM aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) AS acl_item
                 JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
                 LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee
           ), '[]'::jsonb) AS acl,
           obj_description(c.oid, 'pg_class') AS comment
      FROM pg_class AS c
      JOIN pg_namespace AS n ON n.oid = c.relnamespace
      JOIN pg_roles AS owner ON owner.oid = c.relowner
      LEFT JOIN pg_tablespace AS ts ON ts.oid = c.reltablespace
      LEFT JOIN pg_am AS am ON am.oid = c.relam
      LEFT JOIN pg_inherits AS parent_edge ON parent_edge.inhrelid = c.oid
      LEFT JOIN pg_class AS parent ON parent.oid = parent_edge.inhparent
      LEFT JOIN pg_namespace AS parent_namespace ON parent_namespace.oid = parent.relnamespace
     WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
     ORDER BY n.nspname, c.relname, c.relkind
"""

_ATTRIBUTES_SQL = """
    SELECT n.nspname AS schema_name, relation.relname AS relation_name,
           attribute.attnum, attribute.attname, attribute.attisdropped,
           format_type(attribute.atttypid, attribute.atttypmod) AS data_type,
           attribute.atttypmod,
           CASE WHEN attribute.attcollation = 0 THEN NULL
                ELSE attribute.attcollation::regcollation::text END AS collation,
           attribute.attnotnull, attribute.atthasdef, attribute.attidentity,
           attribute.attgenerated, attribute.attstorage, attribute.attcompression,
           attribute.attislocal, attribute.attinhcount, attribute.attstattarget,
           attribute.attlen, attribute.attndims,
           attribute.attoptions, attribute.attfdwoptions, attribute.atthasmissing,
           attribute.attmissingval::text AS missing_value_raw,
           CASE WHEN attribute.attacl IS NULL THEN '[]'::jsonb ELSE COALESCE((
               SELECT jsonb_agg(jsonb_build_object(
                   'grantor', grantor.rolname,
                   'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
                   'privilege', acl_item.privilege_type,
                   'grantable', acl_item.is_grantable
               ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'),
                        acl_item.privilege_type, acl_item.is_grantable)
                 FROM aclexplode(attribute.attacl) AS acl_item
                 JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
                 LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee
           ), '[]'::jsonb) END AS acl,
           pg_get_expr(attrdef.adbin, attrdef.adrelid, true) AS default_expression,
           col_description(attribute.attrelid, attribute.attnum) AS comment
      FROM pg_attribute AS attribute
      JOIN pg_class AS relation ON relation.oid = attribute.attrelid
      JOIN pg_namespace AS n ON n.oid = relation.relnamespace
      LEFT JOIN pg_attrdef AS attrdef
        ON attrdef.adrelid = attribute.attrelid AND attrdef.adnum = attribute.attnum
     WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
       AND attribute.attnum > 0 AND NOT attribute.attisdropped
     ORDER BY n.nspname, relation.relname, attribute.attnum
"""

_CONSTRAINTS_SQL = """
    SELECT COALESCE(n.nspname, type_namespace.nspname) AS schema_name,
           relation.relname AS relation_name, type_row.typname AS domain_name,
           constraint_row.conname, constraint_row.contype,
           constraint_row.condeferrable, constraint_row.condeferred,
           constraint_row.convalidated, constraint_row.connoinherit,
           constraint_row.conislocal, constraint_row.coninhcount,
           CASE WHEN constraint_row.conrelid = 0 THEN NULL
                ELSE constraint_row.conrelid::regclass::text END AS relation_identity,
           CASE WHEN constraint_row.contypid = 0 THEN NULL
                ELSE constraint_row.contypid::regtype::text END AS domain_identity,
           CASE WHEN constraint_row.conindid = 0 THEN NULL
                ELSE constraint_row.conindid::regclass::text END AS index_identity,
           CASE WHEN constraint_row.confrelid = 0 THEN NULL
                ELSE constraint_row.confrelid::regclass::text END AS referenced_relation,
           COALESCE((SELECT jsonb_agg(attribute.attname ORDER BY key_position)
                       FROM unnest(constraint_row.conkey) WITH ORDINALITY AS key_column(attnum, key_position)
                       JOIN pg_attribute AS attribute
                         ON attribute.attrelid = constraint_row.conrelid
                        AND attribute.attnum = key_column.attnum), '[]'::jsonb) AS key_columns,
           COALESCE((SELECT jsonb_agg(attribute.attname ORDER BY key_position)
                       FROM unnest(constraint_row.confkey) WITH ORDINALITY AS key_column(attnum, key_position)
                       JOIN pg_attribute AS attribute
                         ON attribute.attrelid = constraint_row.confrelid
                        AND attribute.attnum = key_column.attnum), '[]'::jsonb) AS referenced_columns,
           constraint_row.confupdtype, constraint_row.confdeltype,
           constraint_row.confmatchtype,
           pg_get_constraintdef(constraint_row.oid, true) AS definition,
           obj_description(constraint_row.oid, 'pg_constraint') AS comment
      FROM pg_constraint AS constraint_row
      LEFT JOIN pg_class AS relation ON relation.oid = constraint_row.conrelid
      LEFT JOIN pg_namespace AS n ON n.oid = relation.relnamespace
      LEFT JOIN pg_type AS type_row ON type_row.oid = constraint_row.contypid
      LEFT JOIN pg_namespace AS type_namespace ON type_namespace.oid = type_row.typnamespace
     WHERE (n.nspname !~ '^pg_' AND n.nspname <> 'information_schema')
        OR (type_namespace.nspname !~ '^pg_' AND type_namespace.nspname <> 'information_schema')
     ORDER BY COALESCE(n.nspname, type_namespace.nspname), relation.relname,
              type_row.typname, constraint_row.conname
"""

_INDEXES_SQL = """
    SELECT n.nspname AS schema_name, relation.relname AS relation_name,
           index_relation.relname AS index_name, index_relation.relpersistence,
           index_row.indisunique, index_row.indisprimary, index_row.indisexclusion,
           index_row.indimmediate, index_row.indisclustered, index_row.indisvalid,
           index_row.indcheckxmin, index_row.indisready, index_row.indislive,
           index_row.indisreplident,
           COALESCE((SELECT jsonb_agg(attribute.attname ORDER BY key_position)
                       FROM unnest(index_row.indkey::smallint[]) WITH ORDINALITY AS key_column(attnum, key_position)
                       LEFT JOIN pg_attribute AS attribute
                         ON attribute.attrelid = index_row.indrelid
                        AND attribute.attnum = key_column.attnum), '[]'::jsonb) AS key_columns,
           COALESCE((SELECT jsonb_agg(
                              CASE WHEN collation_oid = 0 THEN NULL
                                   ELSE collation_oid::regcollation::text END
                              ORDER BY key_position)
                       FROM unnest(index_row.indcollation::oid[]) WITH ORDINALITY
                            AS collation_value(collation_oid, key_position)), '[]'::jsonb) AS collations,
           COALESCE((SELECT jsonb_agg(
                              CASE WHEN opclass.oid IS NULL THEN NULL
                                   ELSE format('%I.%I', opclass_namespace.nspname, opclass.opcname) END
                              ORDER BY key_position)
                       FROM unnest(index_row.indclass::oid[]) WITH ORDINALITY
                            AS opclass_value(opclass_oid, key_position)
                       LEFT JOIN pg_opclass AS opclass ON opclass.oid = opclass_value.opclass_oid
                       LEFT JOIN pg_namespace AS opclass_namespace
                         ON opclass_namespace.oid = opclass.opcnamespace), '[]'::jsonb) AS operator_classes,
           COALESCE((SELECT jsonb_agg(option_value ORDER BY key_position)
                       FROM unnest(index_row.indoption::smallint[]) WITH ORDINALITY
                            AS index_option(option_value, key_position)), '[]'::jsonb) AS options,
           pg_get_expr(index_row.indexprs, index_row.indrelid, true) AS expressions,
           pg_get_expr(index_row.indpred, index_row.indrelid, true) AS predicate,
           pg_get_indexdef(index_row.indexrelid, 0, true) AS definition,
           obj_description(index_row.indexrelid, 'pg_class') AS comment
      FROM pg_index AS index_row
      JOIN pg_class AS relation ON relation.oid = index_row.indrelid
      JOIN pg_class AS index_relation ON index_relation.oid = index_row.indexrelid
      JOIN pg_namespace AS n ON n.oid = relation.relnamespace
     WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
     ORDER BY n.nspname, relation.relname, index_relation.relname
"""

_TRIGGERS_SQL = """
    SELECT n.nspname AS schema_name, relation.relname AS relation_name,
           CASE WHEN trigger_row.tgisinternal THEN NULL ELSE trigger_row.tgname END AS trigger_name,
           trigger_row.tgisinternal AS internal, trigger_row.tgenabled, trigger_row.tgtype,
           trigger_row.tgdeferrable, trigger_row.tginitdeferred,
           trigger_row.tgnargs,
           CASE WHEN trigger_row.tgconstraint = 0 THEN NULL
                ELSE format(
                    '%I.%I',
                    COALESCE(constraint_relation_namespace.nspname, constraint_type_namespace.nspname),
                    constraint_row.conname
                ) END AS constraint_identity,
           CASE WHEN trigger_row.tgconstraint = 0 THEN NULL
                ELSE pg_get_constraintdef(trigger_row.tgconstraint, true) END AS constraint_definition,
           COALESCE((SELECT jsonb_agg(attribute.attname ORDER BY key_position)
                       FROM unnest(trigger_row.tgattr::smallint[]) WITH ORDINALITY
                            AS trigger_column(attnum, key_position)
                       JOIN pg_attribute AS attribute
                         ON attribute.attrelid = trigger_row.tgrelid
                        AND attribute.attnum = trigger_column.attnum), '[]'::jsonb) AS trigger_columns,
           target_namespace.nspname AS target_schema, target_function.proname AS target_name,
           pg_get_function_identity_arguments(target_function.oid) AS target_arguments,
           CASE WHEN trigger_row.tgisinternal THEN NULL ELSE pg_get_triggerdef(trigger_row.oid, true) END AS definition,
           pg_get_expr(trigger_row.tgqual, trigger_row.tgrelid, true) AS when_expression
      FROM pg_trigger AS trigger_row
      JOIN pg_class AS relation ON relation.oid = trigger_row.tgrelid
      JOIN pg_namespace AS n ON n.oid = relation.relnamespace
      JOIN pg_proc AS target_function ON target_function.oid = trigger_row.tgfoid
      JOIN pg_namespace AS target_namespace ON target_namespace.oid = target_function.pronamespace
      LEFT JOIN pg_constraint AS constraint_row ON constraint_row.oid = trigger_row.tgconstraint
      LEFT JOIN pg_class AS constraint_relation ON constraint_relation.oid = constraint_row.conrelid
      LEFT JOIN pg_namespace AS constraint_relation_namespace
        ON constraint_relation_namespace.oid = constraint_relation.relnamespace
      LEFT JOIN pg_type AS constraint_type ON constraint_type.oid = constraint_row.contypid
      LEFT JOIN pg_namespace AS constraint_type_namespace
        ON constraint_type_namespace.oid = constraint_type.typnamespace
     WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
     ORDER BY n.nspname, relation.relname, trigger_row.tgisinternal,
              constraint_identity, trigger_name, trigger_row.tgtype
"""

_REWRITES_SQL = """
    SELECT n.nspname AS schema_name, relation.relname AS relation_name,
           rewrite.rulename, rewrite.ev_type, rewrite.ev_enabled,
           rewrite.is_instead, pg_get_ruledef(rewrite.oid, true) AS definition
      FROM pg_rewrite AS rewrite
      JOIN pg_class AS relation ON relation.oid = rewrite.ev_class
      JOIN pg_namespace AS n ON n.oid = relation.relnamespace
     WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
     ORDER BY n.nspname, relation.relname, rewrite.rulename
"""

_POLICIES_SQL = """
    SELECT n.nspname AS schema_name, relation.relname AS relation_name,
           policy.polname, policy.polpermissive, policy.polcmd,
           COALESCE((SELECT array_agg(role.rolname ORDER BY role.rolname)
                       FROM pg_roles AS role WHERE role.oid = ANY(policy.polroles)), ARRAY[]::name[]) AS roles,
           pg_get_expr(policy.polqual, policy.polrelid, true) AS using_expression,
           pg_get_expr(policy.polwithcheck, policy.polrelid, true) AS check_expression
      FROM pg_policy AS policy
      JOIN pg_class AS relation ON relation.oid = policy.polrelid
      JOIN pg_namespace AS n ON n.oid = relation.relnamespace
     WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
     ORDER BY n.nspname, relation.relname, policy.polname
"""

_FUNCTIONS_SQL = """
    SELECT n.nspname AS schema_name, function_row.proname,
           pg_get_function_identity_arguments(function_row.oid) AS identity_arguments,
           function_row.prokind, language.lanname, function_row.provolatile,
           function_row.proparallel, function_row.proisstrict, function_row.proleakproof,
           function_row.prosecdef, owner.rolname AS owner,
           function_row.prosrc, function_row.probin,
           COALESCE(function_row.proconfig, ARRAY[]::text[]) AS configuration,
           function_row.prosupport::regproc::text AS support_function,
           function_row.pronargdefaults,
           function_row.proargnames::text AS argument_names,
           function_row.proargmodes::text AS argument_modes,
           pg_get_function_arguments(function_row.oid) AS arguments,
           pg_get_function_result(function_row.oid) AS result_type,
           CASE WHEN function_row.prokind IN ('f', 'p')
                THEN pg_get_functiondef(function_row.oid)
                ELSE format('%%s(%%s)', function_row.proname,
                            pg_get_function_identity_arguments(function_row.oid))
           END AS definition,
           CASE WHEN aggregate_row.aggfnoid IS NULL THEN NULL ELSE jsonb_build_object(
               'kind', aggregate_row.aggkind,
               'direct_argument_count', aggregate_row.aggnumdirectargs,
               'transition_function', NULLIF(aggregate_row.aggtransfn, 0)::regproc::text,
               'final_function', NULLIF(aggregate_row.aggfinalfn, 0)::regproc::text,
               'combine_function', NULLIF(aggregate_row.aggcombinefn, 0)::regproc::text,
               'serialize_function', NULLIF(aggregate_row.aggserialfn, 0)::regproc::text,
               'deserialize_function', NULLIF(aggregate_row.aggdeserialfn, 0)::regproc::text,
               'moving_transition_function', NULLIF(aggregate_row.aggmtransfn, 0)::regproc::text,
               'moving_inverse_transition_function', NULLIF(aggregate_row.aggminvtransfn, 0)::regproc::text,
               'moving_final_function', NULLIF(aggregate_row.aggmfinalfn, 0)::regproc::text,
               'final_extra', aggregate_row.aggfinalextra,
               'moving_final_extra', aggregate_row.aggmfinalextra,
               'final_modify', aggregate_row.aggfinalmodify::text,
               'moving_final_modify', aggregate_row.aggmfinalmodify::text,
               'sort_operator', NULLIF(aggregate_row.aggsortop, 0)::regoperator::text,
               'transition_type', aggregate_row.aggtranstype::regtype::text,
               'transition_space', aggregate_row.aggtransspace,
               'moving_transition_type', aggregate_row.aggmtranstype::regtype::text,
               'moving_transition_space', aggregate_row.aggmtransspace,
               'initial_value', aggregate_row.agginitval,
               'moving_initial_value', aggregate_row.aggminitval
           ) END AS aggregate_facts,
           COALESCE((
               SELECT jsonb_agg(jsonb_build_object(
                   'grantor', grantor.rolname,
                   'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
                   'privilege', acl_item.privilege_type,
                   'grantable', acl_item.is_grantable
               ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'),
                        acl_item.privilege_type, acl_item.is_grantable)
                 FROM aclexplode(COALESCE(function_row.proacl, acldefault('f', function_row.proowner))) AS acl_item
                 JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
                 LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee
           ), '[]'::jsonb) AS acl,
           obj_description(function_row.oid, 'pg_proc') AS comment
      FROM pg_proc AS function_row
      JOIN pg_namespace AS n ON n.oid = function_row.pronamespace
      JOIN pg_language AS language ON language.oid = function_row.prolang
      JOIN pg_roles AS owner ON owner.oid = function_row.proowner
      LEFT JOIN pg_aggregate AS aggregate_row ON aggregate_row.aggfnoid = function_row.oid
     WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
     ORDER BY n.nspname, function_row.proname,
              pg_get_function_identity_arguments(function_row.oid)
"""

_TYPES_SQL = """
    SELECT n.nspname AS schema_name, type_row.typname, type_row.typtype,
           owner.rolname AS owner,
           type_row.typlen, type_row.typbyval, type_row.typcategory,
           type_row.typispreferred, type_row.typisdefined, type_row.typdelim,
           type_row.typrelid::regclass::text AS relation_identity,
           CASE WHEN type_row.typsubscript = 0 THEN NULL ELSE type_row.typsubscript::regproc::text END AS subscript,
           CASE WHEN type_row.typelem = 0 THEN NULL ELSE type_row.typelem::regtype::text END AS element_type,
           CASE WHEN type_row.typarray = 0 THEN NULL ELSE type_row.typarray::regtype::text END AS array_type,
           CASE WHEN type_row.typbasetype = 0 THEN NULL ELSE type_row.typbasetype::regtype::text END AS base_type,
           CASE WHEN type_row.typinput = 0 THEN NULL ELSE type_row.typinput::regproc::text END AS input_function,
           CASE WHEN type_row.typoutput = 0 THEN NULL ELSE type_row.typoutput::regproc::text END AS output_function,
           CASE WHEN type_row.typreceive = 0 THEN NULL ELSE type_row.typreceive::regproc::text END AS receive_function,
           CASE WHEN type_row.typsend = 0 THEN NULL ELSE type_row.typsend::regproc::text END AS send_function,
           CASE WHEN type_row.typmodin = 0 THEN NULL ELSE type_row.typmodin::regproc::text END AS typmod_in,
           CASE WHEN type_row.typmodout = 0 THEN NULL ELSE type_row.typmodout::regproc::text END AS typmod_out,
           CASE WHEN type_row.typanalyze = 0 THEN NULL ELSE type_row.typanalyze::regproc::text END AS analyze_function,
           type_row.typalign, type_row.typstorage,
           type_row.typtypmod, type_row.typndims,
           CASE WHEN type_row.typcollation = 0 THEN NULL
                ELSE type_row.typcollation::regcollation::text END AS collation,
           type_row.typnotnull,
           pg_get_expr(type_row.typdefaultbin, 0, true) AS typdefault,
           COALESCE((
               SELECT jsonb_agg(jsonb_build_object(
                   'grantor', grantor.rolname,
                   'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
                   'privilege', acl_item.privilege_type,
                   'grantable', acl_item.is_grantable
               ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'),
                        acl_item.privilege_type, acl_item.is_grantable)
                 FROM aclexplode(COALESCE(type_row.typacl, acldefault('T', type_row.typowner))) AS acl_item
                 JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
                 LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee
           ), '[]'::jsonb) AS acl,
           COALESCE((SELECT jsonb_agg(jsonb_build_object('label', enum.enumlabel, 'sort_order', enum.enumsortorder)
                                      ORDER BY enum.enumsortorder)
                       FROM pg_enum AS enum WHERE enum.enumtypid = type_row.oid), '[]'::jsonb) AS enum_values,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
                           'subtype', range.rngsubtype::regtype::text,
                           'collation', CASE WHEN range.rngcollation = 0 THEN NULL
                                             ELSE range.rngcollation::regcollation::text END,
                           'subtype_opclass', CASE WHEN opclass.oid IS NULL THEN NULL
                                                   ELSE format('%I.%I', opclass_namespace.nspname, opclass.opcname) END,
                           'canonical', range.rngcanonical::regproc::text,
                           'subdiff', range.rngsubdiff::regproc::text))
                       FROM pg_range AS range
                       LEFT JOIN pg_opclass AS opclass ON opclass.oid = range.rngsubopc
                       LEFT JOIN pg_namespace AS opclass_namespace ON opclass_namespace.oid = opclass.opcnamespace
                      WHERE range.rngtypid = type_row.oid), '[]'::jsonb) AS range_values,
           obj_description(type_row.oid, 'pg_type') AS comment
      FROM pg_type AS type_row
      JOIN pg_namespace AS n ON n.oid = type_row.typnamespace
      JOIN pg_roles AS owner ON owner.oid = type_row.typowner
     WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
     ORDER BY n.nspname, type_row.typname
"""

_COMMENTS_SQL = """
    SELECT identified.object_type, identified.object_schema, identified.object_name,
           identified.object_identity, description.description
      FROM pg_description AS description
      CROSS JOIN LATERAL pg_identify_object(
          description.classoid, description.objoid, description.objsubid
      ) AS identified(object_type, object_schema, object_name, object_identity)
     WHERE identified.object_schema !~ '^pg_' AND identified.object_schema <> 'information_schema'
     ORDER BY identified.object_type, identified.object_identity
"""

_SHARED_COMMENTS_SQL = """
    SELECT identified.object_type, identified.object_schema, identified.object_name,
           identified.object_identity, description.description
      FROM pg_shdescription AS description
      CROSS JOIN LATERAL pg_identify_object(
          description.classoid, description.objoid, 0
      ) AS identified(object_type, object_schema, object_name, object_identity)
     ORDER BY identified.object_type, identified.object_identity
"""

_SECURITY_LABELS_SQL = """
    SELECT identified.object_type, identified.object_schema, identified.object_name,
           identified.object_identity, labels.provider, labels.label
      FROM pg_seclabel AS labels
      CROSS JOIN LATERAL pg_identify_object(
          labels.classoid, labels.objoid, labels.objsubid
      ) AS identified(object_type, object_schema, object_name, object_identity)
     ORDER BY identified.object_type, identified.object_identity, labels.provider
"""

_SHARED_SECURITY_LABELS_SQL = """
    SELECT identified.object_type, identified.object_schema, identified.object_name,
           identified.object_identity, labels.provider, labels.label
      FROM pg_shseclabel AS labels
      CROSS JOIN LATERAL pg_identify_object(
          labels.classoid, labels.objoid, 0
      ) AS identified(object_type, object_schema, object_name, object_identity)
     ORDER BY identified.object_type, identified.object_identity, labels.provider
"""

_ACL_COLUMNS_SQL = """
    SELECT n.nspname AS schema_name, relation.relname AS relation_name,
           attribute.attname, CASE WHEN attribute.attacl IS NULL THEN '[]'::jsonb ELSE COALESCE((
               SELECT jsonb_agg(jsonb_build_object(
                   'grantor', grantor.rolname,
                   'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
                   'privilege', acl_item.privilege_type,
                   'grantable', acl_item.is_grantable
               ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'),
                        acl_item.privilege_type, acl_item.is_grantable)
                 FROM aclexplode(attribute.attacl) AS acl_item
                 JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
                 LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee
           ), '[]'::jsonb) END AS acl
      FROM pg_attribute AS attribute
      JOIN pg_class AS relation ON relation.oid = attribute.attrelid
      JOIN pg_namespace AS n ON n.oid = relation.relnamespace
     WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
       AND attribute.attnum > 0 AND NOT attribute.attisdropped
     ORDER BY n.nspname, relation.relname, attribute.attnum
"""

_CASTS_SQL = """
    SELECT cast_row.castsource::regtype::text AS source_type,
           cast_row.casttarget::regtype::text AS target_type,
           CASE WHEN cast_row.castfunc = 0 THEN NULL ELSE cast_row.castfunc::regprocedure::text END AS function,
           cast_row.castcontext, cast_row.castmethod
      FROM pg_cast AS cast_row
     ORDER BY cast_row.castsource::regtype::text, cast_row.casttarget::regtype::text
"""

# Capability catalogs are intentionally separate from the generic relation and
# ACL sections.  They are easy to omit when a discovery implementation starts
# from ``pg_class`` alone, yet they carry cluster-wide authority (for example
# default privileges, event triggers, FDW mappings and logical replication).
# Every query renders object references by stable names and never emits an OID
# or a raw pg_node_tree value.
_PARAMETER_ACLS_SQL = """
    SELECT parameter.parname,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
               'grantor', grantor.rolname,
               'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
               'privilege', acl_item.privilege_type,
               'grantable', acl_item.is_grantable
           ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'),
                     acl_item.privilege_type, acl_item.is_grantable)
             FROM aclexplode(parameter.paracl) AS acl_item
             JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
             LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee), '[]'::jsonb) AS acl
      FROM pg_parameter_acl AS parameter
     ORDER BY parameter.parname
"""

_DEFAULT_ACLS_SQL = """
    SELECT role.rolname AS role_name,
           CASE WHEN default_acl.defaclnamespace = 0 THEN NULL ELSE namespace.nspname END AS schema_name,
           CASE default_acl.defaclobjtype
               WHEN 'r' THEN 'table'
               WHEN 'S' THEN 'sequence'
               WHEN 'f' THEN 'function'
               WHEN 'T' THEN 'type'
               WHEN 'n' THEN 'schema'
               WHEN 'p' THEN 'procedure'
               WHEN 'F' THEN 'routine'
               ELSE default_acl.defaclobjtype::text
           END AS object_type,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
               'grantor', grantor.rolname,
               'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
               'privilege', acl_item.privilege_type,
               'grantable', acl_item.is_grantable
           ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'),
                     acl_item.privilege_type, acl_item.is_grantable)
             FROM aclexplode(default_acl.defaclacl) AS acl_item
             JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
             LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee), '[]'::jsonb) AS acl
      FROM pg_default_acl AS default_acl
      JOIN pg_roles AS role ON role.oid = default_acl.defaclrole
      LEFT JOIN pg_namespace AS namespace ON namespace.oid = default_acl.defaclnamespace
     ORDER BY role.rolname, schema_name NULLS FIRST, object_type
"""

_DB_ROLE_SETTINGS_SQL = """
    SELECT CASE WHEN setting.setdatabase = 0 THEN 'ALL_DATABASES' ELSE database.datname END AS database_name,
           CASE WHEN setting.setrole = 0 THEN 'PUBLIC' ELSE role.rolname END AS role_name,
           COALESCE((SELECT array_agg(config ORDER BY config)
                       FROM unnest(COALESCE(setting.setconfig, ARRAY[]::text[])) AS config),
                    ARRAY[]::text[]) AS settings
      FROM pg_db_role_setting AS setting
      LEFT JOIN pg_database AS database ON database.oid = setting.setdatabase
      LEFT JOIN pg_roles AS role ON role.oid = setting.setrole
     ORDER BY database_name, role_name
"""

_EVENT_TRIGGERS_SQL = """
    SELECT event_trigger.evtname, event_trigger.evtevent AS event,
           owner.rolname AS owner, event_trigger.evtenabled AS enabled,
           COALESCE(event_trigger.evttags, ARRAY[]::text[]) AS tags,
           event_trigger.evtfoid::regprocedure::text AS function_identity
      FROM pg_event_trigger AS event_trigger
      JOIN pg_roles AS owner ON owner.oid = event_trigger.evtowner
     ORDER BY event_trigger.evtname
"""

_LANGUAGES_SQL = """
    SELECT language.lanname, owner.rolname AS owner,
           language.lanispl, language.lanpltrusted,
           CASE WHEN language.lanplcallfoid = 0 THEN NULL
                ELSE language.lanplcallfoid::regproc::text END AS call_handler,
           CASE WHEN language.laninline = 0 THEN NULL
                ELSE language.laninline::regproc::text END AS inline_handler,
           CASE WHEN language.lanvalidator = 0 THEN NULL
                ELSE language.lanvalidator::regproc::text END AS validator,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
               'grantor', grantor.rolname,
               'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
               'privilege', acl_item.privilege_type,
               'grantable', acl_item.is_grantable
           ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'),
                     acl_item.privilege_type, acl_item.is_grantable)
             FROM aclexplode(COALESCE(language.lanacl, acldefault('l', language.lanowner))) AS acl_item
             JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
             LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee), '[]'::jsonb) AS acl
      FROM pg_language AS language
      JOIN pg_roles AS owner ON owner.oid = language.lanowner
     ORDER BY language.lanname
"""

_TABLESPACES_SQL = """
    SELECT tablespace.spcname AS tablespace_name, owner.rolname AS owner,
           COALESCE(tablespace.spcoptions, ARRAY[]::text[]) AS options,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
               'grantor', grantor.rolname,
               'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
               'privilege', acl_item.privilege_type,
               'grantable', acl_item.is_grantable
           ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'),
                     acl_item.privilege_type, acl_item.is_grantable)
             FROM aclexplode(COALESCE(tablespace.spcacl, acldefault('t', tablespace.spcowner))) AS acl_item
             JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
             LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee), '[]'::jsonb) AS acl
      FROM pg_tablespace AS tablespace
      JOIN pg_roles AS owner ON owner.oid = tablespace.spcowner
     ORDER BY tablespace.spcname
"""

_FOREIGN_DATA_SQL = """
    SELECT 'wrapper' AS kind, wrapper.fdwname AS identity, owner.rolname AS owner,
           NULL::text AS server_type, NULL::text AS server_version,
           NULL::text AS mapped_role, NULL::text AS server_name,
           CASE WHEN wrapper.fdwhandler = 0 THEN NULL ELSE wrapper.fdwhandler::regproc::text END AS handler,
           CASE WHEN wrapper.fdwvalidator = 0 THEN NULL ELSE wrapper.fdwvalidator::regproc::text END AS validator,
           COALESCE((SELECT array_agg(option_value ORDER BY option_value)
                       FROM (SELECT option AS option_value
                               FROM unnest(COALESCE(wrapper.fdwoptions, ARRAY[]::text[])) AS option) AS keys),
                    ARRAY[]::text[]) AS option_values,
           '[]'::jsonb AS acl
      FROM pg_foreign_data_wrapper AS wrapper
      JOIN pg_roles AS owner ON owner.oid = wrapper.fdwowner
    UNION ALL
    SELECT 'server' AS kind, server.srvname AS identity, owner.rolname AS owner,
           server.srvtype, server.srvversion, NULL::text AS mapped_role,
           wrapper.fdwname AS server_name, NULL::text AS handler, NULL::text AS validator,
           COALESCE((SELECT array_agg(option_value ORDER BY option_value)
                       FROM (SELECT option AS option_value
                               FROM unnest(COALESCE(server.srvoptions, ARRAY[]::text[])) AS option) AS keys),
                    ARRAY[]::text[]) AS option_values,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
               'grantor', grantor.rolname,
               'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
               'privilege', acl_item.privilege_type,
               'grantable', acl_item.is_grantable
           ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'),
                     acl_item.privilege_type, acl_item.is_grantable)
             FROM aclexplode(server.srvacl) AS acl_item
             JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
             LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee), '[]'::jsonb) AS acl
      FROM pg_foreign_server AS server
      JOIN pg_foreign_data_wrapper AS wrapper ON wrapper.oid = server.srvfdw
      JOIN pg_roles AS owner ON owner.oid = server.srvowner
    UNION ALL
    SELECT 'user_mapping' AS kind,
           server.srvname AS identity, NULL::text AS owner, NULL::text AS server_type,
           NULL::text AS server_version,
           CASE WHEN mapping.umuser = 0 THEN 'PUBLIC' ELSE mapped_role.rolname END AS mapped_role,
           server.srvname AS server_name, NULL::text AS handler, NULL::text AS validator,
           COALESCE((SELECT array_agg(option_value ORDER BY option_value)
                       FROM (SELECT option AS option_value
                               FROM unnest(COALESCE(mapping.umoptions, ARRAY[]::text[])) AS option) AS keys),
                    ARRAY[]::text[]) AS option_values,
           '[]'::jsonb AS acl
      FROM pg_user_mapping AS mapping
      JOIN pg_foreign_server AS server ON server.oid = mapping.umserver
      LEFT JOIN pg_roles AS mapped_role ON mapped_role.oid = mapping.umuser
     ORDER BY kind, identity, mapped_role NULLS FIRST
"""

_LARGE_OBJECTS_SQL = """
    SELECT owner.rolname AS owner,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
               'grantor', grantor.rolname,
               'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
               'privilege', acl_item.privilege_type,
               'grantable', acl_item.is_grantable
           ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'),
                     acl_item.privilege_type, acl_item.is_grantable)
             FROM aclexplode(metadata.lomacl) AS acl_item
             JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
             LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee), '[]'::jsonb) AS acl,
           count(*)::bigint AS object_count
      FROM pg_largeobject_metadata AS metadata
      JOIN pg_roles AS owner ON owner.oid = metadata.lomowner
     GROUP BY owner.rolname, metadata.lomacl
     ORDER BY owner.rolname
"""

_PUBLICATIONS_SQL = """
    SELECT publication.pubname, owner.rolname AS owner,
           publication.puballtables, publication.pubinsert, publication.pubupdate,
           publication.pubdelete, publication.pubtruncate, publication.pubviaroot,
           COALESCE((SELECT jsonb_agg(namespace.nspname ORDER BY namespace.nspname)
                       FROM pg_publication_namespace AS publication_namespace
                       JOIN pg_namespace AS namespace ON namespace.oid = publication_namespace.pnnspid
                      WHERE publication_namespace.pnpubid = publication.oid), '[]'::jsonb) AS schemas,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
               'schema_name', relation_namespace.nspname,
               'relation_name', relation.relname,
               'relation_kind', relation.relkind,
               'is_partition', relation.relispartition,
               'where', pg_get_expr(publication_relation.prqual, publication_relation.prrelid, true),
               'columns', COALESCE((SELECT jsonb_agg(attribute.attname ORDER BY key_position)
                                     FROM unnest(publication_relation.prattrs::smallint[])
                                          WITH ORDINALITY AS key_column(attnum, key_position)
                                     JOIN pg_attribute AS attribute
                                       ON attribute.attrelid = publication_relation.prrelid
                                      AND attribute.attnum = key_column.attnum), '[]'::jsonb)
           ) ORDER BY relation_namespace.nspname, relation.relname)
             FROM pg_publication_rel AS publication_relation
             JOIN pg_class AS relation ON relation.oid = publication_relation.prrelid
             JOIN pg_namespace AS relation_namespace ON relation_namespace.oid = relation.relnamespace
            WHERE publication_relation.prpubid = publication.oid), '[]'::jsonb) AS relations
      FROM pg_publication AS publication
      JOIN pg_roles AS owner ON owner.oid = publication.pubowner
     ORDER BY publication.pubname
"""

_SUBSCRIPTIONS_SQL = """
    SELECT subscription.subname, database.datname AS database_name, owner.rolname AS owner,
           subscription.subenabled, subscription.subbinary, subscription.substream,
           subscription.subtwophasestate, subscription.subdisableonerr,
           subscription.subpasswordrequired, subscription.subrunasowner,
           subscription.subconninfo AS connection_info,
           subscription.subslotname, subscription.subsynccommit, subscription.suborigin,
           COALESCE(subscription.subpublications, ARRAY[]::text[]) AS publications,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
               'schema_name', relation_namespace.nspname,
               'relation_name', relation.relname,
               'relation_kind', relation.relkind,
               'is_partition', relation.relispartition,
               'state', subscription_relation.srsubstate,
               'is_synced', subscription_relation.srsublsn IS NOT NULL)
             ORDER BY relation_namespace.nspname, relation.relname)
             FROM pg_subscription_rel AS subscription_relation
             JOIN pg_class AS relation ON relation.oid = subscription_relation.srrelid
             JOIN pg_namespace AS relation_namespace ON relation_namespace.oid = relation.relnamespace
            WHERE subscription_relation.srsubid = subscription.oid), '[]'::jsonb) AS relations
      FROM pg_subscription AS subscription
      JOIN pg_database AS database ON database.oid = subscription.subdbid
      JOIN pg_roles AS owner ON owner.oid = subscription.subowner
     ORDER BY subscription.subname
"""

_OPERATORS_SQL = """
    SELECT n.nspname AS schema_name, operator_row.oprname,
           format('%I.%I(%s,%s)', n.nspname, operator_row.oprname,
                  operator_row.oprleft::regtype::text, operator_row.oprright::regtype::text) AS identity,
           owner.rolname AS owner, operator_row.oprkind,
           operator_row.oprcanmerge, operator_row.oprcanhash,
           operator_row.oprleft::regtype::text AS left_type,
           operator_row.oprright::regtype::text AS right_type,
           operator_row.oprresult::regtype::text AS result_type,
           CASE WHEN operator_row.oprcom = 0 THEN NULL ELSE operator_row.oprcom::regoperator::text END AS commutator,
           CASE WHEN operator_row.oprnegate = 0 THEN NULL ELSE operator_row.oprnegate::regoperator::text END AS negator,
           CASE WHEN operator_row.oprcode = 0 THEN NULL ELSE operator_row.oprcode::regproc::text END AS procedure,
           CASE WHEN operator_row.oprrest = 0 THEN NULL
                ELSE operator_row.oprrest::regproc::text END AS restrict_function,
           CASE WHEN operator_row.oprjoin = 0 THEN NULL ELSE operator_row.oprjoin::regproc::text END AS join_function,
           obj_description(operator_row.oid, 'pg_operator') AS comment
      FROM pg_operator AS operator_row
      JOIN pg_namespace AS n ON n.oid = operator_row.oprnamespace
      JOIN pg_roles AS owner ON owner.oid = operator_row.oprowner
     WHERE n.nspname !~ '^pg_'
     ORDER BY n.nspname, operator_row.oprname, operator_row.oprleft::regtype::text,
              operator_row.oprright::regtype::text
"""

_OPCLASSES_SQL = """
    SELECT namespace.nspname AS schema_name, opclass.opcname,
           format('%I.%I', namespace.nspname, opclass.opcname) AS identity,
           owner.rolname AS owner, access_method.amname AS access_method,
           family_namespace.nspname AS family_schema, opfamily.opfname AS family_name,
           opclass.opcintype::regtype::text AS input_type,
           CASE WHEN opclass.opckeytype = 0 THEN NULL ELSE opclass.opckeytype::regtype::text END AS key_type,
           opclass.opcdefault,
           obj_description(opclass.oid, 'pg_opclass') AS comment
      FROM pg_opclass AS opclass
      JOIN pg_namespace AS namespace ON namespace.oid = opclass.opcnamespace
      JOIN pg_roles AS owner ON owner.oid = opclass.opcowner
      JOIN pg_am AS access_method ON access_method.oid = opclass.opcmethod
      JOIN pg_opfamily AS opfamily ON opfamily.oid = opclass.opcfamily
      JOIN pg_namespace AS family_namespace ON family_namespace.oid = opfamily.opfnamespace
     WHERE namespace.nspname !~ '^pg_'
     ORDER BY namespace.nspname, opclass.opcname, access_method.amname
"""

_OPFAMILIES_SQL = """
    SELECT namespace.nspname AS schema_name, opfamily.opfname,
           format('%I.%I', namespace.nspname, opfamily.opfname) AS identity,
           owner.rolname AS owner, access_method.amname AS access_method,
           obj_description(opfamily.oid, 'pg_opfamily') AS comment
      FROM pg_opfamily AS opfamily
      JOIN pg_namespace AS namespace ON namespace.oid = opfamily.opfnamespace
      JOIN pg_roles AS owner ON owner.oid = opfamily.opfowner
      JOIN pg_am AS access_method ON access_method.oid = opfamily.opfmethod
     WHERE namespace.nspname !~ '^pg_'
     ORDER BY namespace.nspname, opfamily.opfname, access_method.amname
"""

_COLLATIONS_SQL = """
    SELECT namespace.nspname AS schema_name, collation_row.collname,
           format('%I.%I', namespace.nspname, collation_row.collname) AS identity,
           owner.rolname AS owner, collation_row.collprovider,
           collation_row.collisdeterministic, collation_row.collencoding,
           collation_row.collcollate, collation_row.collctype, collation_row.colliculocale,
           collation_row.collicurules, collation_row.collversion,
           obj_description(collation_row.oid, 'pg_collation') AS comment
      FROM pg_collation AS collation_row
      JOIN pg_namespace AS namespace ON namespace.oid = collation_row.collnamespace
      JOIN pg_roles AS owner ON owner.oid = collation_row.collowner
     WHERE namespace.nspname !~ '^pg_'
     ORDER BY namespace.nspname, collation_row.collname
"""

_CONVERSIONS_SQL = """
    SELECT namespace.nspname AS schema_name, conversion.conname,
           format('%I.%I', namespace.nspname, conversion.conname) AS identity,
           owner.rolname AS owner, conversion.conforencoding,
           conversion.contoencoding,
           conversion.conproc::regproc::text AS conversion_function,
           conversion.condefault,
           obj_description(conversion.oid, 'pg_conversion') AS comment
      FROM pg_conversion AS conversion
      JOIN pg_namespace AS namespace ON namespace.oid = conversion.connamespace
      JOIN pg_roles AS owner ON owner.oid = conversion.conowner
     WHERE namespace.nspname !~ '^pg_'
     ORDER BY namespace.nspname, conversion.conname
"""

_TRANSFORMS_SQL = """
    SELECT type_row.typnamespace::regnamespace::text AS schema_name,
           type_row.oid::regtype::text AS type_identity,
           language.lanname AS language,
           CASE WHEN transform.trffromsql = 0 THEN NULL ELSE transform.trffromsql::regproc::text END AS from_sql,
           CASE WHEN transform.trftosql = 0 THEN NULL ELSE transform.trftosql::regproc::text END AS to_sql,
           obj_description(transform.oid, 'pg_transform') AS comment
      FROM pg_transform AS transform
      JOIN pg_type AS type_row ON type_row.oid = transform.trftype
      JOIN pg_language AS language ON language.oid = transform.trflang
     WHERE type_row.typnamespace::regnamespace::text !~ '^pg_'
     ORDER BY type_row.oid::regtype::text, language.lanname
"""

_TEXT_SEARCH_CONFIGS_SQL = """
    SELECT namespace.nspname AS schema_name, config.cfgname,
           format('%I.%I', namespace.nspname, config.cfgname) AS identity,
           owner.rolname AS owner, parser.prsname AS parser,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
                              'token', map.maptokentype,
                              'sequence', map.mapseqno,
                              'dictionary', dictionary.dictname
                          ) ORDER BY map.mapseqno, map.maptokentype)
                       FROM pg_ts_config_map AS map
                       JOIN pg_ts_dict AS dictionary ON dictionary.oid = map.mapdict
                      WHERE map.mapcfg = config.oid), '[]'::jsonb) AS mappings,
           obj_description(config.oid, 'pg_ts_config') AS comment
      FROM pg_ts_config AS config
      JOIN pg_namespace AS namespace ON namespace.oid = config.cfgnamespace
      JOIN pg_roles AS owner ON owner.oid = config.cfgowner
      JOIN pg_ts_parser AS parser ON parser.oid = config.cfgparser
     WHERE namespace.nspname !~ '^pg_'
     ORDER BY namespace.nspname, config.cfgname
"""

_TEXT_SEARCH_DICTIONARIES_SQL = """
    SELECT namespace.nspname AS schema_name, dictionary.dictname,
           format('%I.%I', namespace.nspname, dictionary.dictname) AS identity,
           owner.rolname AS owner, template.tmplname AS template,
           dictionary.dictinitoption,
           obj_description(dictionary.oid, 'pg_ts_dict') AS comment
      FROM pg_ts_dict AS dictionary
      JOIN pg_namespace AS namespace ON namespace.oid = dictionary.dictnamespace
      JOIN pg_roles AS owner ON owner.oid = dictionary.dictowner
      JOIN pg_ts_template AS template ON template.oid = dictionary.dicttemplate
     WHERE namespace.nspname !~ '^pg_'
     ORDER BY namespace.nspname, dictionary.dictname
"""

_TEXT_SEARCH_PARSERS_SQL = """
    SELECT namespace.nspname AS schema_name, parser.prsname,
           format('%I.%I', namespace.nspname, parser.prsname) AS identity,
           parser.prsstart::regproc::text AS start_function,
           parser.prstoken::regproc::text AS token_function,
           parser.prsend::regproc::text AS end_function,
           parser.prsheadline::regproc::text AS headline_function,
           parser.prslextype::regproc::text AS lexize_function,
           obj_description(parser.oid, 'pg_ts_parser') AS comment
      FROM pg_ts_parser AS parser
      JOIN pg_namespace AS namespace ON namespace.oid = parser.prsnamespace
     WHERE namespace.nspname !~ '^pg_'
     ORDER BY namespace.nspname, parser.prsname
"""

_TEXT_SEARCH_TEMPLATES_SQL = """
    SELECT namespace.nspname AS schema_name, template.tmplname,
           format('%I.%I', namespace.nspname, template.tmplname) AS identity,
           template.tmplinit::regproc::text AS init_function,
           template.tmpllexize::regproc::text AS lexize_function,
           obj_description(template.oid, 'pg_ts_template') AS comment
      FROM pg_ts_template AS template
      JOIN pg_namespace AS namespace ON namespace.oid = template.tmplnamespace
     WHERE namespace.nspname !~ '^pg_'
     ORDER BY namespace.nspname, template.tmplname
"""

_EXTENSION_PG_PROC_FACTS_SQL = """
    SELECT function_row.oid AS member_oid,
           n.nspname AS schema_name, function_row.proname,
           pg_get_function_identity_arguments(function_row.oid) AS identity_arguments,
           function_row.prokind, language.lanname, function_row.provolatile,
           function_row.proparallel, function_row.proisstrict, function_row.proleakproof,
           function_row.prosecdef, owner.rolname AS owner,
           function_row.prosrc, function_row.probin,
           COALESCE(function_row.proconfig, ARRAY[]::text[]) AS configuration,
           function_row.prosupport::regproc::text AS support_function,
           function_row.pronargdefaults, function_row.proargnames::text AS argument_names,
           function_row.proargmodes::text AS argument_modes,
           pg_get_function_arguments(function_row.oid) AS arguments,
           pg_get_function_result(function_row.oid) AS result_type,
           CASE WHEN function_row.prokind IN ('f', 'p')
                THEN pg_get_functiondef(function_row.oid)
                ELSE format('%%s(%%s)', function_row.proname,
                            pg_get_function_identity_arguments(function_row.oid))
           END AS definition,
           CASE WHEN aggregate_row.aggfnoid IS NULL THEN NULL ELSE jsonb_build_object(
               'kind', aggregate_row.aggkind,
               'direct_argument_count', aggregate_row.aggnumdirectargs,
               'transition_function', NULLIF(aggregate_row.aggtransfn, 0)::regproc::text,
               'final_function', NULLIF(aggregate_row.aggfinalfn, 0)::regproc::text,
               'combine_function', NULLIF(aggregate_row.aggcombinefn, 0)::regproc::text,
               'serialize_function', NULLIF(aggregate_row.aggserialfn, 0)::regproc::text,
               'deserialize_function', NULLIF(aggregate_row.aggdeserialfn, 0)::regproc::text,
               'moving_transition_function', NULLIF(aggregate_row.aggmtransfn, 0)::regproc::text,
               'moving_inverse_transition_function', NULLIF(aggregate_row.aggminvtransfn, 0)::regproc::text,
               'moving_final_function', NULLIF(aggregate_row.aggmfinalfn, 0)::regproc::text,
               'final_extra', aggregate_row.aggfinalextra,
               'moving_final_extra', aggregate_row.aggmfinalextra,
               'final_modify', aggregate_row.aggfinalmodify::text,
               'moving_final_modify', aggregate_row.aggmfinalmodify::text,
               'sort_operator', NULLIF(aggregate_row.aggsortop, 0)::regoperator::text,
               'transition_type', aggregate_row.aggtranstype::regtype::text,
               'transition_space', aggregate_row.aggtransspace,
               'moving_transition_type', aggregate_row.aggmtranstype::regtype::text,
               'moving_transition_space', aggregate_row.aggmtransspace,
               'initial_value', aggregate_row.agginitval,
               'moving_initial_value', aggregate_row.aggminitval
           ) END AS aggregate_facts,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
                              'grantor', grantor.rolname,
                              'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
                              'privilege', acl_item.privilege_type,
                              'grantable', acl_item.is_grantable
                          ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'),
                                    acl_item.privilege_type, acl_item.is_grantable)
                       FROM aclexplode(
                                COALESCE(function_row.proacl, acldefault('f', function_row.proowner))
                            ) AS acl_item
                       JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
                       LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee), '[]'::jsonb) AS acl,
           obj_description(function_row.oid, 'pg_proc') AS comment
      FROM pg_proc AS function_row
      JOIN pg_namespace AS n ON n.oid = function_row.pronamespace
      JOIN pg_language AS language ON language.oid = function_row.prolang
      JOIN pg_roles AS owner ON owner.oid = function_row.proowner
      LEFT JOIN pg_aggregate AS aggregate_row ON aggregate_row.aggfnoid = function_row.oid
     WHERE function_row.oid = ANY(%s)
     ORDER BY n.nspname, function_row.proname,
              pg_get_function_identity_arguments(function_row.oid)
"""

_EXTENSION_PG_CLASS_FACTS_SQL = """
    SELECT relation.oid AS member_oid, n.nspname AS schema_name, relation.relname AS object_name,
           relation.relkind, relation.relpersistence, owner.rolname AS owner,
           relation.relreplident, relation.relrowsecurity, relation.relforcerowsecurity,
           relation.relispopulated, access_method.amname AS access_method,
           COALESCE(tablespace.spcname, '') AS tablespace,
           COALESCE(relation.reloptions, ARRAY[]::text[]) AS reloptions,
           relation.relispartition,
           parent_namespace.nspname AS parent_schema, parent.relname AS parent_name,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
                              'grantor', grantor.rolname,
                              'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
                              'privilege', acl_item.privilege_type,
                              'grantable', acl_item.is_grantable
                          ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'),
                                    acl_item.privilege_type, acl_item.is_grantable)
                       FROM aclexplode(COALESCE(relation.relacl, acldefault('r', relation.relowner))) AS acl_item
                       JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
                       LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee), '[]'::jsonb) AS acl,
           CASE WHEN relation.relispartition
                THEN pg_get_expr(relation.relpartbound, relation.oid, true) END AS partition_bound,
           CASE WHEN relation.relkind = 'p' THEN pg_get_partkeydef(relation.oid) END AS partition_key,
           COALESCE((SELECT jsonb_agg(jsonb_build_object('schema', child_namespace.nspname, 'name', child.relname)
                                      ORDER BY child_namespace.nspname, child.relname)
                       FROM pg_inherits AS child_edge
                       JOIN pg_class AS child ON child.oid = child_edge.inhrelid
                       JOIN pg_namespace AS child_namespace ON child_namespace.oid = child.relnamespace
                      WHERE child_edge.inhparent = relation.oid), '[]'::jsonb) AS children,
           obj_description(relation.oid, 'pg_class') AS comment
      FROM pg_class AS relation
      JOIN pg_namespace AS n ON n.oid = relation.relnamespace
      JOIN pg_roles AS owner ON owner.oid = relation.relowner
      LEFT JOIN pg_tablespace AS tablespace ON tablespace.oid = relation.reltablespace
      LEFT JOIN pg_am AS access_method ON access_method.oid = relation.relam
      LEFT JOIN pg_inherits AS parent_edge ON parent_edge.inhrelid = relation.oid
      LEFT JOIN pg_class AS parent ON parent.oid = parent_edge.inhparent
      LEFT JOIN pg_namespace AS parent_namespace ON parent_namespace.oid = parent.relnamespace
     WHERE relation.oid = ANY(%s)
     ORDER BY n.nspname, relation.relname
"""

_EXTENSION_PG_TYPE_FACTS_SQL = """
    SELECT type_row.oid AS member_oid, n.nspname AS schema_name, type_row.typname,
           owner.rolname AS owner, type_row.typtype, type_row.typlen, type_row.typbyval,
           type_row.typcategory, type_row.typispreferred, type_row.typisdefined, type_row.typdelim,
           type_row.typrelid AS relation_oid,
           type_row.typrelid::regclass::text AS relation_identity,
           CASE WHEN type_row.typsubscript = 0 THEN NULL ELSE type_row.typsubscript::regproc::text END AS subscript,
           CASE WHEN type_row.typelem = 0 THEN NULL ELSE type_row.typelem::regtype::text END AS element_type,
           CASE WHEN type_row.typarray = 0 THEN NULL ELSE type_row.typarray::regtype::text END AS array_type,
           CASE WHEN type_row.typbasetype = 0 THEN NULL ELSE type_row.typbasetype::regtype::text END AS base_type,
           CASE WHEN type_row.typinput = 0 THEN NULL ELSE type_row.typinput::regproc::text END AS input_function,
           CASE WHEN type_row.typoutput = 0 THEN NULL ELSE type_row.typoutput::regproc::text END AS output_function,
           CASE WHEN type_row.typreceive = 0 THEN NULL ELSE type_row.typreceive::regproc::text END AS receive_function,
           CASE WHEN type_row.typsend = 0 THEN NULL ELSE type_row.typsend::regproc::text END AS send_function,
           CASE WHEN type_row.typmodin = 0 THEN NULL ELSE type_row.typmodin::regproc::text END AS typmod_in,
           CASE WHEN type_row.typmodout = 0 THEN NULL ELSE type_row.typmodout::regproc::text END AS typmod_out,
           CASE WHEN type_row.typanalyze = 0 THEN NULL ELSE type_row.typanalyze::regproc::text END AS analyze_function,
           type_row.typalign, type_row.typstorage, type_row.typnotnull,
           type_row.typtypmod, type_row.typndims,
           CASE WHEN type_row.typcollation = 0 THEN NULL
                ELSE type_row.typcollation::regcollation::text END AS collation,
           pg_get_expr(type_row.typdefaultbin, 0, true) AS typdefault,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
                              'grantor', grantor.rolname,
                              'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
                              'privilege', acl_item.privilege_type,
                              'grantable', acl_item.is_grantable
                          ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'),
                                    acl_item.privilege_type, acl_item.is_grantable)
                       FROM aclexplode(COALESCE(type_row.typacl, acldefault('T', type_row.typowner))) AS acl_item
                       JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
                       LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee), '[]'::jsonb) AS acl,
           COALESCE((SELECT jsonb_agg(jsonb_build_object('label', enum.enumlabel, 'sort_order', enum.enumsortorder)
                                      ORDER BY enum.enumsortorder)
                       FROM pg_enum AS enum WHERE enum.enumtypid = type_row.oid), '[]'::jsonb) AS enum_values,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
                           'subtype', range.rngsubtype::regtype::text,
                           'collation', CASE WHEN range.rngcollation = 0 THEN NULL
                                             ELSE range.rngcollation::regcollation::text END,
                           'subtype_opclass', CASE WHEN opclass.oid IS NULL THEN NULL
                                                   ELSE format(
                                                       '%%I.%%I', opclass_namespace.nspname, opclass.opcname
                                                   ) END,
                           'canonical', range.rngcanonical::regproc::text,
                           'subdiff', range.rngsubdiff::regproc::text))
                       FROM pg_range AS range
                       LEFT JOIN pg_opclass AS opclass ON opclass.oid = range.rngsubopc
                       LEFT JOIN pg_namespace AS opclass_namespace ON opclass_namespace.oid = opclass.opcnamespace
                      WHERE range.rngtypid = type_row.oid), '[]'::jsonb) AS range_values,
           obj_description(type_row.oid, 'pg_type') AS comment
      FROM pg_type AS type_row
      JOIN pg_namespace AS n ON n.oid = type_row.typnamespace
      JOIN pg_roles AS owner ON owner.oid = type_row.typowner
     WHERE type_row.oid = ANY(%s)
     ORDER BY n.nspname, type_row.typname
"""

_EXTENSION_PG_CAST_FACTS_SQL = """
    SELECT cast_row.oid AS member_oid,
           cast_row.castsource::regtype::text AS source_type,
           cast_row.casttarget::regtype::text AS target_type,
           CASE WHEN cast_row.castfunc = 0 THEN NULL ELSE cast_row.castfunc::regprocedure::text END AS function,
           cast_row.castcontext, cast_row.castmethod
      FROM pg_cast AS cast_row
     WHERE cast_row.oid = ANY(%s)
     ORDER BY cast_row.castsource::regtype::text, cast_row.casttarget::regtype::text
"""

_EXTENSION_PG_OPERATOR_FACTS_SQL = """
    SELECT operator_row.oid AS member_oid, n.nspname AS schema_name, operator_row.oprname,
           owner.rolname AS owner, operator_row.oprkind, operator_row.oprcanmerge,
           operator_row.oprcanhash, operator_row.oprleft::regtype::text AS left_type,
           operator_row.oprright::regtype::text AS right_type,
           operator_row.oprresult::regtype::text AS result_type,
           CASE WHEN operator_row.oprcom = 0 THEN NULL ELSE operator_row.oprcom::regoperator::text END AS commutator,
           CASE WHEN operator_row.oprnegate = 0 THEN NULL ELSE operator_row.oprnegate::regoperator::text END AS negator,
           CASE WHEN operator_row.oprcode = 0 THEN NULL ELSE operator_row.oprcode::regproc::text END AS procedure,
           CASE WHEN operator_row.oprrest = 0 THEN NULL
                ELSE operator_row.oprrest::regproc::text END AS restrict_function,
           CASE WHEN operator_row.oprjoin = 0 THEN NULL ELSE operator_row.oprjoin::regproc::text END AS join_function,
           obj_description(operator_row.oid, 'pg_operator') AS comment
      FROM pg_operator AS operator_row
      JOIN pg_namespace AS n ON n.oid = operator_row.oprnamespace
      JOIN pg_roles AS owner ON owner.oid = operator_row.oprowner
     WHERE operator_row.oid = ANY(%s)
     ORDER BY n.nspname, operator_row.oprname
"""

_EXTENSION_PG_OPFAMILY_FACTS_SQL = """
    SELECT opfamily.oid AS member_oid, namespace.nspname AS schema_name, opfamily.opfname,
           owner.rolname AS owner, access_method.amname AS access_method,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
                              'left_type', amop.amoplefttype::regtype::text,
                              'right_type', amop.amoprighttype::regtype::text,
                              'strategy', amop.amopstrategy,
                              'purpose', amop.amoppurpose,
                              'operator', amop.amopopr::regoperator::text,
                              'method', amop_method.amname,
                              'sort_family', CASE WHEN sort_family.oid IS NULL THEN NULL
                                                  ELSE format(
                                                      '%%I.%%I', sort_namespace.nspname, sort_family.opfname
                                                  ) END
                          ) ORDER BY amop.amoplefttype::regtype::text, amop.amoprighttype::regtype::text,
                                    amop.amopstrategy, amop.amoppurpose)
                       FROM pg_amop AS amop
                       JOIN pg_am AS amop_method ON amop_method.oid = amop.amopmethod
                       LEFT JOIN pg_opfamily AS sort_family ON sort_family.oid = amop.amopsortfamily
                       LEFT JOIN pg_namespace AS sort_namespace ON sort_namespace.oid = sort_family.opfnamespace
                      WHERE amop.amopfamily = opfamily.oid), '[]'::jsonb) AS operators,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
                              'left_type', amproc.amproclefttype::regtype::text,
                              'right_type', amproc.amprocrighttype::regtype::text,
                              'number', amproc.amprocnum,
                              'procedure', amproc.amproc::regproc::text
                          ) ORDER BY amproc.amproclefttype::regtype::text, amproc.amprocrighttype::regtype::text,
                                    amproc.amprocnum)
                       FROM pg_amproc AS amproc
                      WHERE amproc.amprocfamily = opfamily.oid), '[]'::jsonb) AS procedures,
           obj_description(opfamily.oid, 'pg_opfamily') AS comment
      FROM pg_opfamily AS opfamily
      JOIN pg_namespace AS namespace ON namespace.oid = opfamily.opfnamespace
      JOIN pg_roles AS owner ON owner.oid = opfamily.opfowner
      JOIN pg_am AS access_method ON access_method.oid = opfamily.opfmethod
     WHERE opfamily.oid = ANY(%s)
     ORDER BY namespace.nspname, opfamily.opfname
"""

_EXTENSION_PG_OPCLASS_FACTS_SQL = """
    SELECT opclass.oid AS member_oid, namespace.nspname AS schema_name, opclass.opcname,
           owner.rolname AS owner, access_method.amname AS access_method,
           family_namespace.nspname AS family_schema, opfamily.opfname AS family_name,
           opclass.opcintype::regtype::text AS input_type,
           CASE WHEN opclass.opckeytype = 0 THEN NULL ELSE opclass.opckeytype::regtype::text END AS key_type,
           opclass.opcdefault, obj_description(opclass.oid, 'pg_opclass') AS comment
      FROM pg_opclass AS opclass
      JOIN pg_namespace AS namespace ON namespace.oid = opclass.opcnamespace
      JOIN pg_roles AS owner ON owner.oid = opclass.opcowner
      JOIN pg_am AS access_method ON access_method.oid = opclass.opcmethod
      JOIN pg_opfamily AS opfamily ON opfamily.oid = opclass.opcfamily
      JOIN pg_namespace AS family_namespace ON family_namespace.oid = opfamily.opfnamespace
     WHERE opclass.oid = ANY(%s)
     ORDER BY namespace.nspname, opclass.opcname
"""

_EXTENSION_PG_LANGUAGE_FACTS_SQL = """
    SELECT language.oid AS member_oid, language.lanname, owner.rolname AS owner,
           language.lanispl, language.lanpltrusted,
           CASE WHEN language.lanplcallfoid = 0 THEN NULL
                ELSE language.lanplcallfoid::regproc::text END AS call_handler,
           CASE WHEN language.laninline = 0 THEN NULL ELSE language.laninline::regproc::text END AS inline_handler,
           CASE WHEN language.lanvalidator = 0 THEN NULL ELSE language.lanvalidator::regproc::text END AS validator,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
                              'grantor', grantor.rolname,
                              'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
                              'privilege', acl_item.privilege_type,
                              'grantable', acl_item.is_grantable
                          ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'),
                                    acl_item.privilege_type, acl_item.is_grantable)
                       FROM aclexplode(COALESCE(language.lanacl, acldefault('l', language.lanowner))) AS acl_item
                       JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
                       LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee), '[]'::jsonb) AS acl,
           obj_description(language.oid, 'pg_language') AS comment
      FROM pg_language AS language
      JOIN pg_roles AS owner ON owner.oid = language.lanowner
     WHERE language.oid = ANY(%s)
     ORDER BY language.lanname
"""

# Recursive extension-member closure.  These queries intentionally use the
# member OIDs only as an internal selector; every returned row is converted to
# schema-qualified semantic identities before it can reach canonical facts.
_EXTENSION_PG_CLASS_ATTRIBUTES_SQL = """
    SELECT n.nspname AS schema_name, relation.relname AS relation_name,
           attribute.attnum, attribute.attname, attribute.attisdropped,
           format_type(attribute.atttypid, attribute.atttypmod) AS data_type,
           attribute.atttypmod,
           CASE WHEN attribute.attcollation = 0 THEN NULL
                ELSE attribute.attcollation::regcollation::text END AS collation,
           attribute.attnotnull, attribute.atthasdef, attribute.attidentity,
           attribute.attgenerated, attribute.attstorage, attribute.attcompression,
           attribute.attislocal, attribute.attinhcount, attribute.attstattarget,
           attribute.attlen, attribute.attndims, attribute.attoptions,
           attribute.attfdwoptions, attribute.atthasmissing,
           attribute.attmissingval::text AS missing_value_raw,
           COALESCE((SELECT jsonb_agg(jsonb_build_object(
               'grantor', grantor.rolname,
               'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
               'privilege', acl_item.privilege_type,
               'grantable', acl_item.is_grantable
           ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'),
                     acl_item.privilege_type, acl_item.is_grantable)
             FROM aclexplode(attribute.attacl) AS acl_item
             JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
             LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee), '[]'::jsonb) AS acl,
           pg_get_expr(attrdef.adbin, attrdef.adrelid, true) AS default_expression,
           col_description(attribute.attrelid, attribute.attnum) AS comment
      FROM pg_attribute AS attribute
      JOIN pg_class AS relation ON relation.oid = attribute.attrelid
      JOIN pg_namespace AS n ON n.oid = relation.relnamespace
      LEFT JOIN pg_attrdef AS attrdef
        ON attrdef.adrelid = attribute.attrelid AND attrdef.adnum = attribute.attnum
     WHERE attribute.attrelid = ANY(%s) AND attribute.attnum > 0 AND NOT attribute.attisdropped
     ORDER BY n.nspname, relation.relname, attribute.attnum
"""

_EXTENSION_PG_CLASS_CONSTRAINTS_SQL = """
    SELECT COALESCE(n.nspname, type_namespace.nspname) AS schema_name,
           relation.relname AS relation_name, type_row.typname AS domain_name,
           constraint_row.conname, constraint_row.contype,
           constraint_row.condeferrable, constraint_row.condeferred,
           constraint_row.convalidated, constraint_row.connoinherit,
           constraint_row.conislocal, constraint_row.coninhcount,
           CASE WHEN constraint_row.conrelid = 0 THEN NULL
                ELSE constraint_row.conrelid::regclass::text END AS relation_identity,
           CASE WHEN constraint_row.contypid = 0 THEN NULL
                ELSE constraint_row.contypid::regtype::text END AS domain_identity,
           CASE WHEN constraint_row.conindid = 0 THEN NULL
                ELSE constraint_row.conindid::regclass::text END AS index_identity,
           CASE WHEN constraint_row.confrelid = 0 THEN NULL
                ELSE constraint_row.confrelid::regclass::text END AS referenced_relation,
           COALESCE((SELECT jsonb_agg(attribute.attname ORDER BY key_position)
                       FROM unnest(constraint_row.conkey) WITH ORDINALITY AS key_column(attnum, key_position)
                       JOIN pg_attribute AS attribute
                         ON attribute.attrelid = constraint_row.conrelid
                        AND attribute.attnum = key_column.attnum), '[]'::jsonb) AS key_columns,
           COALESCE((SELECT jsonb_agg(attribute.attname ORDER BY key_position)
                       FROM unnest(constraint_row.confkey) WITH ORDINALITY AS key_column(attnum, key_position)
                       JOIN pg_attribute AS attribute
                         ON attribute.attrelid = constraint_row.confrelid
                        AND attribute.attnum = key_column.attnum), '[]'::jsonb) AS referenced_columns,
           constraint_row.confupdtype, constraint_row.confdeltype, constraint_row.confmatchtype,
           pg_get_constraintdef(constraint_row.oid, true) AS definition,
           obj_description(constraint_row.oid, 'pg_constraint') AS comment
      FROM pg_constraint AS constraint_row
      LEFT JOIN pg_class AS relation ON relation.oid = constraint_row.conrelid
      LEFT JOIN pg_namespace AS n ON n.oid = relation.relnamespace
      LEFT JOIN pg_type AS type_row ON type_row.oid = constraint_row.contypid
      LEFT JOIN pg_namespace AS type_namespace ON type_namespace.oid = type_row.typnamespace
     WHERE constraint_row.conrelid = ANY(%s) OR constraint_row.conindid = ANY(%s)
     ORDER BY COALESCE(n.nspname, type_namespace.nspname), relation.relname,
              type_row.typname, constraint_row.conname
"""

_EXTENSION_PG_CLASS_INDEXES_SQL = """
    SELECT n.nspname AS schema_name, relation.relname AS relation_name,
           index_relation.relname AS index_name, index_relation.relpersistence,
           index_row.indisunique, index_row.indisprimary, index_row.indisexclusion,
           index_row.indimmediate, index_row.indisclustered, index_row.indisvalid,
           index_row.indcheckxmin, index_row.indisready, index_row.indislive,
           index_row.indisreplident,
           COALESCE((SELECT jsonb_agg(attribute.attname ORDER BY key_position)
                       FROM unnest(index_row.indkey::smallint[])
                            WITH ORDINALITY AS key_column(attnum, key_position)
                       LEFT JOIN pg_attribute AS attribute
                         ON attribute.attrelid = index_row.indrelid
                        AND attribute.attnum = key_column.attnum), '[]'::jsonb) AS key_columns,
           COALESCE((SELECT jsonb_agg(
                              CASE WHEN collation_oid = 0 THEN NULL
                                   ELSE collation_oid::regcollation::text END
                              ORDER BY key_position)
                       FROM unnest(index_row.indcollation::oid[])
                            WITH ORDINALITY AS collation_value(collation_oid, key_position)),
                    '[]'::jsonb) AS collations,
           COALESCE((SELECT jsonb_agg(
                              CASE WHEN opclass.oid IS NULL THEN NULL
                                   ELSE format('%%I.%%I', opclass_namespace.nspname, opclass.opcname) END
                              ORDER BY key_position)
                       FROM unnest(index_row.indclass::oid[])
                            WITH ORDINALITY AS opclass_value(opclass_oid, key_position)
                       LEFT JOIN pg_opclass AS opclass ON opclass.oid = opclass_value.opclass_oid
                       LEFT JOIN pg_namespace AS opclass_namespace
                         ON opclass_namespace.oid = opclass.opcnamespace), '[]'::jsonb) AS operator_classes,
           COALESCE((SELECT jsonb_agg(option_value ORDER BY key_position)
                       FROM unnest(index_row.indoption::smallint[])
                            WITH ORDINALITY AS index_option(option_value, key_position)), '[]'::jsonb) AS options,
           pg_get_expr(index_row.indexprs, index_row.indrelid, true) AS expressions,
           pg_get_expr(index_row.indpred, index_row.indrelid, true) AS predicate,
           pg_get_indexdef(index_row.indexrelid, 0, true) AS definition,
           obj_description(index_row.indexrelid, 'pg_class') AS comment
      FROM pg_index AS index_row
      JOIN pg_class AS relation ON relation.oid = index_row.indrelid
      JOIN pg_class AS index_relation ON index_relation.oid = index_row.indexrelid
      JOIN pg_namespace AS n ON n.oid = relation.relnamespace
     WHERE index_row.indrelid = ANY(%s) OR index_row.indexrelid = ANY(%s)
     ORDER BY n.nspname, relation.relname, index_relation.relname
"""

_EXTENSION_PG_CLASS_TRIGGERS_SQL = """
    SELECT n.nspname AS schema_name, relation.relname AS relation_name,
           CASE WHEN trigger_row.tgisinternal THEN NULL ELSE trigger_row.tgname END AS trigger_name,
           trigger_row.tgisinternal AS internal, trigger_row.tgenabled, trigger_row.tgtype,
           trigger_row.tgdeferrable, trigger_row.tginitdeferred, trigger_row.tgnargs,
           CASE WHEN trigger_row.tgconstraint = 0 THEN NULL
                ELSE format(
                    '%%I.%%I',
                    COALESCE(constraint_relation_namespace.nspname, constraint_type_namespace.nspname),
                    constraint_row.conname
                ) END AS constraint_identity,
           CASE WHEN trigger_row.tgconstraint = 0 THEN NULL
                ELSE pg_get_constraintdef(trigger_row.tgconstraint, true) END AS constraint_definition,
           COALESCE((SELECT jsonb_agg(attribute.attname ORDER BY key_position)
                       FROM unnest(trigger_row.tgattr::smallint[])
                            WITH ORDINALITY AS trigger_column(attnum, key_position)
                       JOIN pg_attribute AS attribute
                         ON attribute.attrelid = trigger_row.tgrelid
                        AND attribute.attnum = trigger_column.attnum), '[]'::jsonb) AS trigger_columns,
           target_namespace.nspname AS target_schema, target_function.proname AS target_name,
           pg_get_function_identity_arguments(target_function.oid) AS target_arguments,
           CASE WHEN trigger_row.tgisinternal THEN NULL ELSE pg_get_triggerdef(trigger_row.oid, true) END AS definition,
           pg_get_expr(trigger_row.tgqual, trigger_row.tgrelid, true) AS when_expression
      FROM pg_trigger AS trigger_row
      JOIN pg_class AS relation ON relation.oid = trigger_row.tgrelid
      JOIN pg_namespace AS n ON n.oid = relation.relnamespace
      JOIN pg_proc AS target_function ON target_function.oid = trigger_row.tgfoid
      JOIN pg_namespace AS target_namespace ON target_namespace.oid = target_function.pronamespace
      LEFT JOIN pg_constraint AS constraint_row ON constraint_row.oid = trigger_row.tgconstraint
      LEFT JOIN pg_class AS constraint_relation ON constraint_relation.oid = constraint_row.conrelid
      LEFT JOIN pg_namespace AS constraint_relation_namespace
        ON constraint_relation_namespace.oid = constraint_relation.relnamespace
      LEFT JOIN pg_type AS constraint_type ON constraint_type.oid = constraint_row.contypid
      LEFT JOIN pg_namespace AS constraint_type_namespace
        ON constraint_type_namespace.oid = constraint_type.typnamespace
     WHERE trigger_row.tgrelid = ANY(%s)
     ORDER BY n.nspname, relation.relname, trigger_row.tgisinternal,
              constraint_identity, trigger_name, trigger_row.tgtype
"""

_EXTENSION_PG_CLASS_REWRITES_SQL = """
    SELECT n.nspname AS schema_name, relation.relname AS relation_name,
           rewrite.rulename, rewrite.ev_type, rewrite.ev_enabled,
           rewrite.is_instead, pg_get_ruledef(rewrite.oid, true) AS definition
      FROM pg_rewrite AS rewrite
      JOIN pg_class AS relation ON relation.oid = rewrite.ev_class
      JOIN pg_namespace AS n ON n.oid = relation.relnamespace
     WHERE rewrite.ev_class = ANY(%s)
     ORDER BY n.nspname, relation.relname, rewrite.rulename
"""

_EXTENSION_PG_CLASS_POLICIES_SQL = """
    SELECT n.nspname AS schema_name, relation.relname AS relation_name,
           policy.polname, policy.polpermissive, policy.polcmd,
           COALESCE((SELECT array_agg(role.rolname ORDER BY role.rolname)
                       FROM pg_roles AS role WHERE role.oid = ANY(policy.polroles)), ARRAY[]::name[]) AS roles,
           pg_get_expr(policy.polqual, policy.polrelid, true) AS using_expression,
           pg_get_expr(policy.polwithcheck, policy.polrelid, true) AS check_expression
      FROM pg_policy AS policy
      JOIN pg_class AS relation ON relation.oid = policy.polrelid
      JOIN pg_namespace AS n ON n.oid = relation.relnamespace
     WHERE policy.polrelid = ANY(%s)
     ORDER BY n.nspname, relation.relname, policy.polname
"""

_EXTENSION_PG_CLASS_SEQUENCES_SQL = """
    SELECT n.nspname AS schema_name, relation.relname AS relation_name,
           sequence.seqtypid::regtype::text AS data_type,
           sequence.seqstart, sequence.seqincrement, sequence.seqmax,
           sequence.seqmin, sequence.seqcache, sequence.seqcycle
      FROM pg_sequence AS sequence
      JOIN pg_class AS relation ON relation.oid = sequence.seqrelid
      JOIN pg_namespace AS n ON n.oid = relation.relnamespace
     WHERE sequence.seqrelid = ANY(%s)
     ORDER BY n.nspname, relation.relname
"""

_EXTENSION_PG_TYPE_ATTRIBUTES_SQL = _EXTENSION_PG_CLASS_ATTRIBUTES_SQL

_EXTENSION_PG_TYPE_CONSTRAINTS_SQL = """
    SELECT type_namespace.nspname AS schema_name, type_row.typname AS domain_name,
           constraint_row.conname, constraint_row.contype,
           constraint_row.convalidated, pg_get_constraintdef(constraint_row.oid, true) AS definition,
           obj_description(constraint_row.oid, 'pg_constraint') AS comment
      FROM pg_constraint AS constraint_row
      JOIN pg_type AS type_row ON type_row.oid = constraint_row.contypid
      JOIN pg_namespace AS type_namespace ON type_namespace.oid = type_row.typnamespace
     WHERE constraint_row.contypid = ANY(%s)
     ORDER BY type_namespace.nspname, type_row.typname, constraint_row.conname
"""

_EXTENSION_CLASS_RECURSIVE_SQL: dict[str, tuple[str, ...]] = {
    "pg_class": (
        _EXTENSION_PG_CLASS_ATTRIBUTES_SQL,
        _EXTENSION_PG_CLASS_CONSTRAINTS_SQL,
        _EXTENSION_PG_CLASS_INDEXES_SQL,
        _EXTENSION_PG_CLASS_TRIGGERS_SQL,
        _EXTENSION_PG_CLASS_REWRITES_SQL,
        _EXTENSION_PG_CLASS_POLICIES_SQL,
        _EXTENSION_PG_CLASS_SEQUENCES_SQL,
    ),
    "pg_proc": (_EXTENSION_PG_PROC_FACTS_SQL,),
    "pg_type": (_EXTENSION_PG_TYPE_ATTRIBUTES_SQL, _EXTENSION_PG_TYPE_CONSTRAINTS_SQL),
    "pg_cast": (_EXTENSION_PG_CAST_FACTS_SQL,),
    "pg_operator": (_EXTENSION_PG_OPERATOR_FACTS_SQL,),
    "pg_opfamily": (_EXTENSION_PG_OPFAMILY_FACTS_SQL,),
    "pg_opclass": (_EXTENSION_PG_OPCLASS_FACTS_SQL,),
    "pg_language": (_EXTENSION_PG_LANGUAGE_FACTS_SQL,),
}

_SUPPORTED_EXTENSION_MEMBER_CLASSES = {
    "pg_proc",
    "pg_class",
    "pg_type",
    "pg_cast",
    "pg_operator",
    "pg_opfamily",
    "pg_opclass",
    "pg_language",
}


def _extension_member_semantics(
    cursor: psycopg.Cursor[Any], members: list[dict[str, Any]]
) -> dict[tuple[str, int], dict[str, Any]]:
    """Compile every baseline extension member class into stable semantic facts."""

    by_class: dict[str, list[int]] = {}
    for member in members:
        class_name = str(member["member_class"])
        if class_name not in _SUPPORTED_EXTENSION_MEMBER_CLASSES:
            raise CatalogAuthorityError(f"unsupported extension member catalog class: {class_name}")
        by_class.setdefault(class_name, []).append(int(member["member_oid"]))
    statements = {
        "pg_proc": _EXTENSION_PG_PROC_FACTS_SQL,
        "pg_class": _EXTENSION_PG_CLASS_FACTS_SQL,
        "pg_type": _EXTENSION_PG_TYPE_FACTS_SQL,
        "pg_cast": _EXTENSION_PG_CAST_FACTS_SQL,
        "pg_operator": _EXTENSION_PG_OPERATOR_FACTS_SQL,
        "pg_opfamily": _EXTENSION_PG_OPFAMILY_FACTS_SQL,
        "pg_opclass": _EXTENSION_PG_OPCLASS_FACTS_SQL,
        "pg_language": _EXTENSION_PG_LANGUAGE_FACTS_SQL,
    }
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for class_name, oids in by_class.items():
        for row in _rows(cursor, statements[class_name], (oids,)):
            oid = int(row.pop("member_oid"))
            if (class_name, oid) in result:
                raise CatalogAuthorityError(f"duplicate extension member semantic fact: {class_name}:{oid}")
            if class_name == "pg_proc":
                definition = str(row.get("definition") or "")
                row["definition_sha256"] = _sha256(definition.encode())
                row.pop("definition", None)
            if class_name == "pg_type":
                relation_oids = []
                relation_oid = row.pop("relation_oid", None)
                if isinstance(relation_oid, int) and relation_oid > 0:
                    relation_oids = [relation_oid]
                row["recursive_facts"] = {
                    "composite_attributes": _normalize_extension_attributes(
                        _rows(cursor, _EXTENSION_PG_TYPE_ATTRIBUTES_SQL, (relation_oids,))
                    )
                    if relation_oids
                    else [],
                    "domain_constraints": _rows(cursor, _EXTENSION_PG_TYPE_CONSTRAINTS_SQL, ([oid],)),
                }
            elif class_name == "pg_class":
                # A member's closure is scoped to that member relation.  Using
                # the complete class OID list here would make every extension
                # member inherit unrelated relation facts and would turn a
                # mutation in one relation into a false change for all peers.
                row["recursive_facts"] = _extension_relation_recursive_facts(cursor, [oid])
            result[(class_name, oid)] = row
    missing = {
        (str(member["member_class"]), int(member["member_oid"]))
        for member in members
        if (str(member["member_class"]), int(member["member_oid"])) not in result
    }
    if missing:
        raise CatalogAuthorityError(f"extension member semantic facts are incomplete: {sorted(missing)!r}")
    return result


def _normalize_extension_attributes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply the same attribute/default policy to recursive member facts."""

    for row in rows:
        row["missing_value"] = _canonical_missing_value(row)
        row.pop("missing_value_raw", None)
    return rows


def _extension_relation_recursive_facts(
    cursor: psycopg.Cursor[Any], relation_oids: list[int]
) -> dict[str, list[dict[str, Any]]]:
    """Read the complete relation-owned semantic closure for an extension member."""

    return {
        "attributes": _normalize_extension_attributes(
            _rows(cursor, _EXTENSION_PG_CLASS_ATTRIBUTES_SQL, (relation_oids,))
        ),
        "constraints": _rows(cursor, _EXTENSION_PG_CLASS_CONSTRAINTS_SQL, (relation_oids, relation_oids)),
        "indexes": _rows(cursor, _EXTENSION_PG_CLASS_INDEXES_SQL, (relation_oids, relation_oids)),
        "triggers": _rows(cursor, _EXTENSION_PG_CLASS_TRIGGERS_SQL, (relation_oids,)),
        "rewrites": _rows(cursor, _EXTENSION_PG_CLASS_REWRITES_SQL, (relation_oids,)),
        "policies": _rows(cursor, _EXTENSION_PG_CLASS_POLICIES_SQL, (relation_oids,)),
        "sequences": _rows(cursor, _EXTENSION_PG_CLASS_SEQUENCES_SQL, (relation_oids,)),
    }


def _extension_rows(cursor: psycopg.Cursor[Any]) -> list[dict[str, Any]]:
    extensions = _rows(cursor, _EXTENSIONS_SQL)
    members = _rows(cursor, _EXTENSION_MEMBERS_SQL)
    semantic_facts = _extension_member_semantics(cursor, members)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for member in members:
        definition = member.pop("semantic_definition")
        class_name = str(member.pop("member_class"))
        member_oid = int(member.pop("member_oid"))
        member["semantic_definition_sha256"] = _sha256(str(definition or "").encode())
        member["semantic_facts"] = semantic_facts[(class_name, member_oid)]
        by_name.setdefault(str(member.pop("extname")), []).append(member)
    for extension in extensions:
        name = str(extension["extname"])
        extension["members"] = _sort_rows(by_name.get(name, []))
    return extensions


def _function_rows(cursor: psycopg.Cursor[Any]) -> list[dict[str, Any]]:
    functions = _rows(cursor, _FUNCTIONS_SQL)
    for function in functions:
        definition = str(function.get("definition") or "")
        function["definition_sha256"] = _sha256(definition.encode())
    return functions


def _redacted_option_facts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hash complete FDW option values while keeping secrets out of evidence."""

    for row in rows:
        raw_options = row.pop("option_values", [])
        if not isinstance(raw_options, list) or not all(isinstance(option, str) for option in raw_options):
            raise CatalogAuthorityError("foreign-data option catalog facts are malformed")
        facts: list[dict[str, Any]] = []
        for option in raw_options:
            key, separator, value = option.partition("=")
            facts.append(
                {
                    "key": key,
                    "has_value": bool(separator),
                    "value_sha256": _sha256(value.encode()),
                    "option_sha256": _sha256(option.encode()),
                }
            )
        row["options"] = _sort_rows(facts)
    return rows


def _redacted_subscription_facts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hash subscription connection strings without emitting connection secrets."""

    for row in rows:
        connection_info = row.pop("connection_info", None)
        if connection_info is not None and not isinstance(connection_info, str):
            raise CatalogAuthorityError("subscription connection catalog fact is malformed")
        row["has_connection_info"] = bool(connection_info)
        row["connection_info_sha256"] = _sha256(connection_info.encode()) if isinstance(connection_info, str) else None
    return rows


def _canonical_missing_value(row: dict[str, Any]) -> dict[str, Any] | None:
    """Classify fast-default storage without treating evaluated data as schema."""

    if not bool(row.get("atthasmissing")):
        return None
    expression = row.get("default_expression")
    data_type = row.get("data_type")
    raw_value = row.get("missing_value_raw")
    if isinstance(expression, str) and _LITERAL_DEFAULT_RE.fullmatch(expression):
        return {"mode": "literal", "type": data_type, "value": raw_value}
    return {
        "mode": "dynamic_default_expression",
        "type": data_type,
        "expression": expression,
    }


def _attribute_rows(cursor: psycopg.Cursor[Any]) -> list[dict[str, Any]]:
    rows = _rows(cursor, _ATTRIBUTES_SQL)
    return _normalize_extension_attributes(rows)


def _build_acl(
    functions: list[dict[str, Any]],
    objects: list[dict[str, Any]],
    attributes: list[dict[str, Any]],
    types: list[dict[str, Any]],
    schemas: list[dict[str, Any]],
    database: list[dict[str, Any]],
    columns: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "database": _sort_rows(database),
        "schemas": _sort_rows(schemas),
        "relations": _sort_rows(objects),
        "columns": _sort_rows(columns),
        "functions": _sort_rows(functions),
        "attribute_facts": _sort_rows(attributes),
        "types": _sort_rows(types),
    }


def _build_ownership(
    functions: list[dict[str, Any]],
    objects: list[dict[str, Any]],
    types: list[dict[str, Any]],
    schemas: list[dict[str, Any]],
    database: list[dict[str, Any]],
    casts: list[dict[str, Any]],
    operators: list[dict[str, Any]],
    opclasses: list[dict[str, Any]],
    opfamilies: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "database": _sort_rows(database),
        "schemas": _sort_rows(schemas),
        "relations": _sort_rows(objects),
        "functions": _sort_rows(functions),
        "types": _sort_rows(types),
        "casts": _sort_rows(casts),
        "operators": _sort_rows(operators),
        "opclasses": _sort_rows(opclasses),
        "opfamilies": _sort_rows(opfamilies),
    }


def compile_catalog_authority(
    compatibility_rows: list[dict[str, Any]],
    extensions: list[dict[str, Any]],
    namespaces: list[dict[str, Any]],
    database: list[dict[str, Any]],
    roles: list[dict[str, Any]],
    memberships: list[dict[str, Any]],
    objects: list[dict[str, Any]],
    attributes: list[dict[str, Any]],
    constraints: list[dict[str, Any]],
    indexes: list[dict[str, Any]],
    triggers: list[dict[str, Any]],
    rewrites: list[dict[str, Any]],
    policies: list[dict[str, Any]],
    functions: list[dict[str, Any]],
    types: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    security_labels: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    casts: list[dict[str, Any]],
    operators: list[dict[str, Any]],
    opclasses: list[dict[str, Any]],
    opfamilies: list[dict[str, Any]],
    collations: list[dict[str, Any]],
    conversions: list[dict[str, Any]],
    transforms: list[dict[str, Any]],
    text_search: list[dict[str, Any]],
    parameter_acls: list[dict[str, Any]] | None = None,
    default_acls: list[dict[str, Any]] | None = None,
    db_role_settings: list[dict[str, Any]] | None = None,
    event_triggers: list[dict[str, Any]] | None = None,
    languages: list[dict[str, Any]] | None = None,
    tablespaces: list[dict[str, Any]] | None = None,
    foreign_data: list[dict[str, Any]] | None = None,
    large_objects: list[dict[str, Any]] | None = None,
    publications: list[dict[str, Any]] | None = None,
    subscriptions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compile collected facts into canonical section roots without a database."""

    compatibility = compatibility_rows[0] if len(compatibility_rows) == 1 else compatibility_rows
    sections: dict[str, list[dict[str, Any]] | dict[str, Any]] = {
        "compatibility": [compatibility] if isinstance(compatibility, dict) else compatibility,
        "extensions": extensions,
        "namespaces": namespaces,
        "database": database,
        "roles": _sort_rows(roles + memberships),
        "objects": objects,
        "attributes": attributes,
        "constraints": constraints,
        "indexes": indexes,
        "triggers": triggers,
        "rewrites": rewrites,
        "policies": policies,
        "functions": functions,
        "types": types,
        "acl": _build_acl(functions, objects, attributes, types, namespaces, database, columns),
        "ownership": _build_ownership(
            functions,
            objects,
            types,
            namespaces,
            database,
            casts,
            operators,
            opclasses,
            opfamilies,
        ),
        "comments": comments,
        "security_labels": security_labels,
        "extension_dependencies": [
            {"extension": extension["extname"], "members": extension["members"]} for extension in extensions
        ],
        "casts": casts,
        "operators": operators,
        "opclasses": opclasses,
        "opfamilies": opfamilies,
        "collations": collations,
        "conversions": conversions,
        "transforms": transforms,
        "text_search": text_search,
        "parameter_acls": parameter_acls or [],
        "default_acls": default_acls or [],
        "db_role_settings": db_role_settings or [],
        "event_triggers": event_triggers or [],
        "languages": languages or [],
        "tablespaces": tablespaces or [],
        "foreign_data": foreign_data or [],
        "large_objects": large_objects or [],
        "publications": publications or [],
        "subscriptions": subscriptions or [],
    }
    _reject_physical_catalog_keys(sections, ast_context=True)
    summary = {name: _section(list(value) if isinstance(value, list) else [value]) for name, value in sections.items()}
    compatibility_value = sections["compatibility"]
    overall_payload = {
        "compiler_version": CATALOG_AUTHORITY_COMPILER_VERSION,
        "compatibility": compatibility_value,
        "sections": summary,
    }
    return {
        **overall_payload,
        "overall_root": _sha256(canonical_bytes(overall_payload)),
        "section_facts": sections,
    }


def discover_catalog_authority(connection: psycopg.Connection[Any]) -> dict[str, Any]:
    """Discover all public catalog roots without expected-name prefiltering.

    The query set intentionally starts from ``pg_class``/``pg_proc``/``pg_type``
    universes and only applies the public namespace boundary.  It never uses an
    expected relation/function registry, name-allowlist predicate or extension
    exclusion as an input filter.  OIDs and volatile physical statistics are
    excluded only where a stable identity/semantic definition is recorded.
    """

    with connection.cursor() as cursor:
        # Make every regclass/regtype/regproc rendering independent of the
        # caller's search_path.  User-defined identities remain
        # schema-qualified while pg_catalog built-ins retain their stable
        # canonical short names.
        cursor.execute("SET search_path TO pg_catalog")
        compatibility_rows = _rows(cursor, _COMPATIBILITY_SQL)
        extensions = _extension_rows(cursor)
        namespaces = _rows(cursor, _NAMESPACES_SQL)
        database = _rows(cursor, _DATABASE_SQL)
        roles = _rows(cursor, _ROLES_SQL)
        memberships = _rows(cursor, _MEMBERSHIPS_SQL)
        objects = _rows(cursor, _OBJECTS_SQL)
        attributes = _attribute_rows(cursor)
        constraints = _rows(cursor, _CONSTRAINTS_SQL)
        indexes = _rows(cursor, _INDEXES_SQL)
        triggers = _rows(cursor, _TRIGGERS_SQL)
        rewrites = _rows(cursor, _REWRITES_SQL)
        policies = _rows(cursor, _POLICIES_SQL)
        functions = _function_rows(cursor)
        types = _rows(cursor, _TYPES_SQL)
        comments = _rows(cursor, _COMMENTS_SQL) + _rows(cursor, _SHARED_COMMENTS_SQL)
        security_labels = _rows(cursor, _SECURITY_LABELS_SQL) + _rows(cursor, _SHARED_SECURITY_LABELS_SQL)
        columns = _acl_entries(cursor, _ACL_COLUMNS_SQL)
        casts = _rows(cursor, _CASTS_SQL)
        operators = _rows(cursor, _OPERATORS_SQL)
        opclasses = _rows(cursor, _OPCLASSES_SQL)
        opfamilies = _rows(cursor, _OPFAMILIES_SQL)
        collations = _rows(cursor, _COLLATIONS_SQL)
        conversions = _rows(cursor, _CONVERSIONS_SQL)
        transforms = _rows(cursor, _TRANSFORMS_SQL)
        text_search = (
            [{"kind": "config", **row} for row in _rows(cursor, _TEXT_SEARCH_CONFIGS_SQL)]
            + [{"kind": "dictionary", **row} for row in _rows(cursor, _TEXT_SEARCH_DICTIONARIES_SQL)]
            + [{"kind": "parser", **row} for row in _rows(cursor, _TEXT_SEARCH_PARSERS_SQL)]
            + [{"kind": "template", **row} for row in _rows(cursor, _TEXT_SEARCH_TEMPLATES_SQL)]
        )
        parameter_acls = _rows(cursor, _PARAMETER_ACLS_SQL)
        default_acls = _rows(cursor, _DEFAULT_ACLS_SQL)
        db_role_settings = _rows(cursor, _DB_ROLE_SETTINGS_SQL)
        event_triggers = _rows(cursor, _EVENT_TRIGGERS_SQL)
        languages = _rows(cursor, _LANGUAGES_SQL)
        tablespaces = _rows(cursor, _TABLESPACES_SQL)
        foreign_data = _redacted_option_facts(_rows(cursor, _FOREIGN_DATA_SQL))
        large_objects = _rows(cursor, _LARGE_OBJECTS_SQL)
        publications = _rows(cursor, _PUBLICATIONS_SQL)
        subscriptions = _redacted_subscription_facts(_rows(cursor, _SUBSCRIPTIONS_SQL))

    return compile_catalog_authority(
        compatibility_rows,
        extensions,
        namespaces,
        database,
        roles,
        memberships,
        objects,
        attributes,
        constraints,
        indexes,
        triggers,
        rewrites,
        policies,
        functions,
        types,
        comments,
        security_labels,
        columns,
        casts,
        operators,
        opclasses,
        opfamilies,
        collations,
        conversions,
        transforms,
        text_search,
        parameter_acls,
        default_acls,
        db_role_settings,
        event_triggers,
        languages,
        tablespaces,
        foreign_data,
        large_objects,
        publications,
        subscriptions,
    )


_PHYSICAL_AST_KEYS = frozenset(
    {
        "node",
        "ast",
        "varno",
        "varattno",
        "varlevelsup",
        "varcollid",
        "consttype",
        "consttypmod",
        "constcollid",
        "opno",
        "opfuncid",
        "funcid",
        "refobjid",
        "refclassid",
        "objid",
        "raw_expression",
        "node_text",
    }
)
_PHYSICAL_AST_TEXT_RE = re.compile(
    r"(?:\b(?:varno|varattno|varlevelsup|varcollid|consttype|consttypmod|constcollid|"
    r"opno|opfuncid|funcid|refobjid|refclassid|objid)\b\s*[:=])",
    re.IGNORECASE,
)


def _reject_physical_catalog_keys(value: object, *, ast_context: bool = True) -> None:
    """Reject physical OID fields and PostgreSQL internal AST representations.

    The compiler deliberately renders semantic definitions with ``pg_get_*``.
    A caller that smuggles a raw node tree into an artifact must not be able to
    make the self-hash pass merely by recomputing ``overall_root``.  Keep this
    recursive guard independent of the artifact's own hash and apply it to both
    live facts and external evidence.
    """

    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower()
            if "oid" in normalized or "filenode" in normalized:
                raise CatalogAuthorityError(f"physical catalog key is not allowed in expected artifact: {key}")
            if ast_context and (
                normalized in _PHYSICAL_AST_KEYS or normalized.endswith("_node") or normalized.endswith("_ast")
            ):
                raise CatalogAuthorityError(f"physical catalog AST is not allowed in expected artifact: {key}")
            _reject_physical_catalog_keys(item, ast_context=ast_context)
    elif isinstance(value, list):
        for item in value:
            _reject_physical_catalog_keys(item, ast_context=ast_context)
    elif ast_context and isinstance(value, str) and _PHYSICAL_AST_TEXT_RE.search(value):
        raise CatalogAuthorityError("physical catalog AST is not allowed in expected artifact")


def _validate_artifact(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "compiler_version",
        "compatibility",
        "sections",
        "overall_root",
        "issuer",
    }:
        raise CatalogAuthorityError("catalog authority expected artifact has an invalid shape")
    if value["compiler_version"] != CATALOG_AUTHORITY_COMPILER_VERSION:
        raise CatalogAuthorityError("catalog authority compiler version mismatch")
    if value["issuer"] != "source-controlled-grove-catalog-authority":
        raise CatalogAuthorityError("catalog authority expected artifact issuer mismatch")
    _reject_physical_catalog_keys(value, ast_context=True)
    if not isinstance(value["sections"], dict) or not isinstance(value["overall_root"], str):
        raise CatalogAuthorityError("catalog authority expected artifact roots are malformed")
    if set(value["sections"]) != set(_SECTION_NAMES):
        raise CatalogAuthorityError("catalog authority expected artifact sections are incomplete")
    for section_name, section in value["sections"].items():
        if not isinstance(section, dict) or set(section) != {"count", "root"}:
            raise CatalogAuthorityError(f"catalog authority expected section summary is malformed: {section_name}")
        if not isinstance(section["count"], int) or section["count"] < 0 or not isinstance(section["root"], str):
            raise CatalogAuthorityError(f"catalog authority expected section summary is malformed: {section_name}")
    overall_payload = {
        "compiler_version": value["compiler_version"],
        "compatibility": value["compatibility"],
        "sections": value["sections"],
    }
    if value["overall_root"] != _sha256(canonical_bytes(overall_payload)):
        raise CatalogAuthorityError("catalog authority expected artifact self-hash mismatch")
    return value


def _read_expected_artifact(path: Path) -> dict[str, Any]:
    """Read external evidence and enforce the independent source anchor."""

    if not path.is_file() or path.is_symlink():
        raise CatalogAuthorityError("catalog authority expected artifact is missing or not regular")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CatalogAuthorityError("catalog authority expected artifact cannot be read") from exc
    is_source_anchor = path.resolve() == CATALOG_AUTHORITY_EXPECTED_ARTIFACT.resolve()
    if is_source_anchor and _sha256(raw) != CATALOG_AUTHORITY_EXPECTED_ARTIFACT_SHA256:
        raise CatalogAuthorityError("catalog authority expected artifact external hash anchor mismatch")
    try:
        artifact = _validate_artifact(json.loads(raw))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogAuthorityError("catalog authority expected artifact is not valid JSON") from exc
    if is_source_anchor and artifact["overall_root"] != CATALOG_AUTHORITY_EXPECTED_ROOT:
        raise CatalogAuthorityError("catalog authority expected root external anchor mismatch")
    return artifact


def compare_expected_catalog_root(
    actual: dict[str, Any], expected_path: Path = CATALOG_AUTHORITY_EXPECTED_ARTIFACT
) -> dict[str, Any]:
    """Compare a live root with external expected facts without self-healing."""

    expected = _read_expected_artifact(expected_path)
    actual_payload = {
        "compiler_version": actual.get("compiler_version"),
        "compatibility": actual.get("compatibility"),
        "sections": actual.get("sections"),
    }
    actual_root = actual.get("overall_root")
    if actual_root != _sha256(canonical_bytes(actual_payload)):
        raise CatalogAuthorityError("live catalog authority root self-hash mismatch")
    if (
        actual_payload
        != {
            "compiler_version": expected["compiler_version"],
            "compatibility": expected["compatibility"],
            "sections": expected["sections"],
        }
        or actual_root != expected["overall_root"]
    ):
        raise CatalogAuthorityError("expected catalog authority root mismatch")
    return actual


def expected_catalog_artifact_hash(path: Path = CATALOG_AUTHORITY_EXPECTED_ARTIFACT) -> str:
    """Return the source-controlled artifact hash; never rewrite the artifact."""

    _read_expected_artifact(path)
    if path.resolve() == CATALOG_AUTHORITY_EXPECTED_ARTIFACT.resolve():
        return CATALOG_AUTHORITY_EXPECTED_ARTIFACT_SHA256
    return _sha256(path.read_bytes())


def expected_catalog_authority_root(path: Path = CATALOG_AUTHORITY_EXPECTED_ARTIFACT) -> str:
    """Read the externally fixed overall root without deriving a replacement."""

    artifact = _read_expected_artifact(path)
    if path.resolve() == CATALOG_AUTHORITY_EXPECTED_ARTIFACT.resolve():
        return CATALOG_AUTHORITY_EXPECTED_ROOT
    return str(artifact["overall_root"])


def expected_catalog_authority_sections(path: Path = CATALOG_AUTHORITY_EXPECTED_ARTIFACT) -> dict[str, dict[str, Any]]:
    """Return externally anchored section summaries for Manifest binding."""

    artifact = _read_expected_artifact(path)
    return cast(dict[str, dict[str, Any]], json.loads(json.dumps(artifact["sections"])))


def trusted_catalog_authority_anchor() -> dict[str, Any]:
    """Re-read the source-controlled catalog anchor for a verification boundary.

    The code-fixed compiler/version, artifact digest and root are independent
    trust facts.  Reading the artifact here also revalidates its shape and
    self-hash, so callers never authenticate a Manifest or report from their
    own self-declared catalog fields.
    """

    artifact = _read_expected_artifact(CATALOG_AUTHORITY_EXPECTED_ARTIFACT)
    return {
        "compiler_version": CATALOG_AUTHORITY_COMPILER_VERSION,
        "artifact_hash": CATALOG_AUTHORITY_EXPECTED_ARTIFACT_SHA256,
        "expected_root": CATALOG_AUTHORITY_EXPECTED_ROOT,
        "sections": cast(dict[str, dict[str, Any]], json.loads(json.dumps(artifact["sections"]))),
    }
