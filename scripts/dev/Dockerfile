# Linux simulator environment for the ChiselSim/Verilator test suite.
#
# This image deliberately contains only build/test tooling. The repository is
# bind-mounted by test.ps1, so Windows remains the single working checkout and
# Vivado continues to run on the host.
# Build a pinned Verilator release instead of using Noble's older 5.020 package.
# ChiselSim invokes Verilator with `-j 0`; 5.020 can intermittently abort while
# destroying its internal thread pool after an otherwise successful build.
FROM eclipse-temurin:21-jdk-noble

ARG SBT_VERSION=1.12.13
ARG VERILATOR_VERSION=5.050

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        autoconf \
        bison \
        ca-certificates \
        curl \
        flex \
        g++ \
        help2man \
        libfl-dev \
        make \
        perl \
        python3 \
    && curl --fail --location --silent --show-error \
        "https://github.com/verilator/verilator/archive/refs/tags/v${VERILATOR_VERSION}.tar.gz" \
        | tar --extract --gzip --directory /tmp \
    && cd "/tmp/verilator-${VERILATOR_VERSION}" \
    && autoconf \
    && ./configure --prefix=/opt/verilator \
    && make -j "$(nproc)" \
    && make install \
    && ln --symbolic /opt/verilator/bin/verilator /usr/local/bin/verilator \
    && curl --fail --location --silent --show-error \
        "https://github.com/sbt/sbt/releases/download/v${SBT_VERSION}/sbt-${SBT_VERSION}.tgz" \
        | tar --extract --gzip --directory /opt \
    && ln --symbolic /opt/sbt/bin/sbt /usr/local/bin/sbt \
    && rm --recursive --force "/tmp/verilator-${VERILATOR_VERSION}" \
    && rm --recursive --force /var/lib/apt/lists/*

WORKDIR /workspace

# Keep non-interactive test output stable and let test.ps1 place tool caches in
# a named Docker volume instead of the bind-mounted source tree.
ENV CI=true
ENTRYPOINT ["sbt"]
CMD ["test"]
