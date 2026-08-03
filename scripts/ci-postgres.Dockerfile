ARG PGVECTOR_IMAGE=pgvector/pgvector:pg16@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b
ARG POSTGIS_IMAGE=postgis/postgis:16-3.5@sha256:4be58fcb1b50df187e73536e663149c2b3b2da2a541c2f518cfb6adebc65ed91

FROM ${PGVECTOR_IMAGE} AS vector
FROM ${POSTGIS_IMAGE}

ARG PGVECTOR_IMAGE
ARG POSTGIS_IMAGE

LABEL org.grove.pgvector-source="${PGVECTOR_IMAGE}" \
      org.grove.postgis-source="${POSTGIS_IMAGE}"

COPY --from=vector /usr/lib/postgresql/16/lib/vector.so /usr/lib/postgresql/16/lib/vector.so
COPY --from=vector /usr/share/postgresql/16/extension/vector* /usr/share/postgresql/16/extension/
