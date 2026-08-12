# Repository notes

## npm publishing authentication

- Prefer passkey-based browser authentication: run `npm login --auth-type=web`, copy the URL printed in the terminal, and open it manually in a browser. Do not assume that the browser opens automatically.
- After completing the passkey flow in the browser, verify the CLI session with `npm whoami`.
- A successful web login does not satisfy a separate publish-time OTP policy. If `npm publish --access public` returns `EOTP`, report that distinction accurately instead of claiming that web authentication failed.
- Do not ask for or handle an authenticator OTP unless npm still explicitly requires one after web login. `npm publish` itself has no `--auth-type=web` option.
