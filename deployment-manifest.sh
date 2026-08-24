#!/usr/bin/env bash
# Canonical runtime closure and deployment target topology for deploy.sh.
# Keep target-owned environments, credentials, state, and provider auth files out.

DEPLOY_FILES=(
  requirements.txt
  package.json
  package-lock.json
  server.py
  mobile_terminal_config.py
  proxy.py
  proxy_auth.py
  webauthn_auth.py
  provider_authority.py
  provider_binding_hook.py
  install_provider_hooks.py
  static/app.js
  static/passkey.js
  static/styles.css
  static/index.html
  static/sw.js
)

# npm ci materializes these locked runtime assets inside the remote staging tree.
DEPLOY_GENERATED_FILES=(
  node_modules/@xterm/xterm/css/xterm.css
  node_modules/@xterm/xterm/lib/xterm.js
  node_modules/@xterm/addon-fit/lib/addon-fit.js
  node_modules/@xterm/addon-serialize/lib/addon-serialize.js
)

DEPLOY_PYTHON_FILES=(
  server.py
  mobile_terminal_config.py
  proxy.py
  proxy_auth.py
  webauthn_auth.py
  provider_authority.py
  provider_binding_hook.py
  install_provider_hooks.py
)

DEPLOY_JAVASCRIPT_FILES=(
  static/app.js
  static/passkey.js
  static/sw.js
)

DEPLOY_DEPENDENCY_IMPORTS=(
  cryptography
  PIL
  regex
  wcwidth
  webauthn
  websockets
)

# name | gate | ssh target | ssh user | runtime user | repository | interpreter | service scope | exact service | health port
# The order is the rollout order. There are deliberately no ps/lat fleet aliases.
DEPLOY_TARGETS=(
  "ps-powerhouse|ps|powerhouse@powerspec|powerhouse|powerhouse|/home/powerhouse/mobile-terminal|/home/powerhouse/mobile-terminal/.venv/bin/python|user-systemd|mobile-terminal.service|8085"
  "lat-ben|lat|ubuntu@100.88.210.92|ubuntu|ben|/home/ben/mobile-terminal|/home/ben/mobile-terminal/.venv/bin/python|system-systemd|mobile-terminal@ben.service|8086"
  "lat-bperritt|lat|ubuntu@100.88.210.92|ubuntu|bperritt|/home/bperritt/mobile-terminal|/home/bperritt/mobile-terminal/.venv/bin/python|system-systemd|mobile-terminal@bperritt.service|8085"
  "mbp-powerhouse|optional|powerhouse@100.80.7.0|powerhouse|powerhouse|/Users/powerhouse/mobile-terminal|/Users/powerhouse/mobile-terminal/.venv/bin/python|launchd|com.mobile-terminal.server|8085"
)
