# Ephemeral GitHub Actions runner on the Playwright base image (ADR-0039):
# browser OS dependencies come preinstalled, so nothing is apt-installed on the host.
ARG PLAYWRIGHT_TAG=v1.58.2-noble
FROM mcr.microsoft.com/playwright:${PLAYWRIGHT_TAG}

ARG RUNNER_VERSION=2.338.2
ARG RUNNER_SHA256=baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl jq git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 10002 -s /bin/bash runner

WORKDIR /home/runner/actions-runner
RUN curl -fsSL -o runner.tgz "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
 && echo "${RUNNER_SHA256}  runner.tgz" | sha256sum -c - \
 && tar xzf runner.tgz && rm runner.tgz \
 && ./bin/installdependencies.sh \
 && rm -rf /var/lib/apt/lists/* \
 && chown -R runner:runner /home/runner

COPY --chown=runner:runner entrypoint.sh /home/runner/entrypoint.sh
RUN chmod 0755 /home/runner/entrypoint.sh
USER runner
ENV RUNNER_ALLOW_RUNASROOT=0 \
    PLAYWRIGHT_BROWSERS_PATH=/home/runner/pw-browsers
ENTRYPOINT ["/home/runner/entrypoint.sh"]
