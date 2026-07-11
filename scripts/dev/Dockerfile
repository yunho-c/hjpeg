# Linux simulator environment for the ChiselSim/Verilator test suite.
#
# This image deliberately contains only build/test tooling. The repository is
# bind-mounted by test.ps1, so Windows remains the single working checkout and
# Vivado continues to run on the host.
# Noble supplies a current Verilator that accepts ChiselSim's `-j 0` option,
# matching the Ubuntu generation used by the CI workflow.
FROM eclipse-temurin:21-jdk-noble

ARG SBT_VERSION=1.12.13

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        g++ \
        make \
        perl \
        verilator \
    && curl --fail --location --silent --show-error \
        "https://github.com/sbt/sbt/releases/download/v${SBT_VERSION}/sbt-${SBT_VERSION}.tgz" \
        | tar --extract --gzip --directory /opt \
    && ln --symbolic /opt/sbt/bin/sbt /usr/local/bin/sbt \
    && rm --recursive --force /var/lib/apt/lists/*

WORKDIR /workspace

# Keep non-interactive test output stable and let test.ps1 place tool caches in
# a named Docker volume instead of the bind-mounted source tree.
ENV CI=true
ENTRYPOINT ["sbt"]
CMD ["test"]
