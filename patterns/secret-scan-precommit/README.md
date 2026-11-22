# secret-scan-precommit

A key pasted into a file and committed before anyone looked.

## What it fixes

When an assistant is allowed to run `git add` and `git commit` by itself, no
person reads the diff, and a pasted key reaches the remote. This is a git
pre-commit hook that reads only the lines the commit adds and refuses the commit
if one looks like a secret. It does not rely on anyone's attention.

It checks three things:

1. The path. `.env` and its variants (`.env.local`, `.env.production.local`, `.env.local.bak-123`),
   `prod.env`, `.envrc`, `.dev.vars` (and `.dev.vars.production`) and private key files are refused.
   A name ending `.example`, `.sample` or `.template` (`.env.example`) is fine.
2. The shape. Anthropic, OpenAI, GitHub, AWS, Stripe, Slack and Google keys,
   npm and SendGrid keys, private-key blocks, JWTs, and `password = <something real>`
   style lines, including prefixed or suffixed names (`DB_PASSWORD`, `client_secret`,
   `OPENAI_API_KEY`, `clientSecret`). Placeholders (`your-key-here`, `process.env.X`, `${X}`), dates and
   environment-variable names are not flagged.
3. The value. If you point it at a notes file or a folder of `.env` files, any
   secret in them is refused anywhere it shows up in a staged line, whatever its shape.

It never prints the value. You get `file:line` and the name of the rule.

## What you need

- python3, 3.9 or newer
- git
- nothing else; no other pattern is required

## Install

By hand, for one repo:

    mkdir -p ~/.claude/hubkit/secret-scan-precommit
    cp secret-scan.py install-hook.sh ~/.claude/hubkit/secret-scan-precommit/
    bash ~/.claude/hubkit/secret-scan-precommit/install-hook.sh /path/to/your/repo

`install-hook.sh` writes `.git/hooks/pre-commit` (or wherever your `core.hooksPath`
points). It never overwrites a hook that is already there: it prints the one line to add
to yours instead. A hook only counts as already wired if it holds that exact line with no
`exit` above it; a comment or a mention does not. Running it twice does nothing the second time. It does not touch
`settings.json`.

## What you will see

    BLOCKED by secret-scan pre-commit hook: the staged change adds what looks like a secret. Values are not shown.
      notes.md:3: (rule: github-token)

Remove the value, read it from an environment variable instead, `git add` again, commit
again.

## Settings (all optional)

| variable | what it does |
|----------|--------------|
| `SECRET_SCAN_VALUE_FILES` | colon-separated files (a notes file with logins). Key-like values in them are blocked everywhere |
| `SECRET_SCAN_ENV_ROOTS` | colon-separated folders whose `.env` files are read. Default: the repo being committed |
| `SECRET_SCAN_ALLOW_PATHS` | colon-separated repo paths that hold made-up secret-shaped text on purpose, like this pattern's own tests |

Set them in your shell profile, or at the top of `.git/hooks/pre-commit`. Nothing is exempt by default.

## Limits you should know

- It reads only what the commit adds. A secret already in your history is not found by this.
- It can only catch the shapes it knows and the values you point it at. A made-up
  internal token with no recognizable shape is invisible unless it is in a value file.
- `git commit --no-verify` skips every hook. Do not use it on a flagged line.
- If the scan itself crashes, or any git command it runs fails, the commit is blocked and the
  message says the scan failed. A git that cannot answer is never read as "nothing to scan".
- It reads the staged file contents directly, so your diff settings (`diff.noprefix`,
  `diff.mnemonicPrefix`, textconv, `.gitattributes` binary markers) cannot hide a line.
  A file that contains NUL bytes (binary, UTF-16, UTF-32) is scanned WHOLE, not just the added
  part, so an old secret in a binary file you edit will block the commit.
- A name that merely contains a keyword (`token_count`, `secret_name`) is flagged only when
  the value has both letters and digits, so settings names do not cry wolf.

## Test it

    bash test.sh            # 50 tests in throwaway repos (about 50 seconds)
    bash test.sh --mutate   # also breaks each guard and checks a test fails

The test file builds every secret-shaped fixture at run time, so the file itself
carries none.

## Lift just this piece

Copy `secret-scan.py` and `install-hook.sh`. Nothing else in this repo is needed.
