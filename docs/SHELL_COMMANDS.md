# Shell Commands (`!`) in the Chat Composer

Execute shell commands directly from the chat composer using the `!` prefix, similar to Claude Code's bang command syntax.

## Basic Usage

### Four Ways to Use Bang Commands

1. **`! <command>`** — Run a command in a new shell
   ```
   ! echo hello
   ! ls -la
   ```
   Creates a new shell of the agent's default declared type and executes the command. In native sessions, that default is typically your `$SHELL`.

2. **`!<shell_key> <command>`** — Run in an existing shell
   ```
   !u-ab12cd pwd
   ```
   Sends the command to a specific running shell by its displayed key (or full resource ID), preserving its working directory and environment.

3. **`!<shell_type> <command>`** — Run in a new shell of a specific type
   ```
   !zsh echo $SHELL
   !bash ls
   ```
   Creates a new shell of the specified type and runs the command.

4. **`!`** (bare) — Open a new shell
   ```
   !
   ```
   Opens a new default shell without executing any command (same as clicking "+ New shell" in the Shells tab).

## Autocomplete Menu

Type `!` in the composer to open the autocomplete dropdown when at least one user shell or multiple shell types are available:

- **Running Shells** (top section) — Lists user shells with their address keys (e.g., `u-ab12cd`); exited shells remain visible but cannot be selected
- **New shell…** (bottom section) — Shows available shell types to create

Navigate with **↑/↓** arrow keys, complete with **Tab** or **Enter**, dismiss with **Escape**.

The types section is hidden when only one shell type is available (since bare `!` already creates that type).

## Command Receipts

Every command-bearing bang submission creates a receipt card in the conversation showing:

- The command that was run
- Which shell it ran in (e.g., `→ zsh · u-ab12cd`)
- Whether it was a new shell (`→ new zsh`) or an existing one

Receipts persist across page reloads and are visible to all session viewers. They do **not** capture command output — view output in the live shell terminal in the right rail.

## Shell State Preservation

Commands sent to an existing shell via `!u-xxxxx` preserve that shell's state:

- Current working directory
- Environment variables
- Command history

This lets you chain commands across multiple bang submissions. Use the new shell key shown in the receipt or menu:

```
! cd /tmp
!u-ab12cd pwd          # Shows /tmp
!u-ab12cd echo $PWD    # Also /tmp
```

## Edge Cases & Escaping

### Literal `!` in Chat

To send a message starting with `!` to the agent (not as a shell command), use one of:

- **Backslash escape**: `\!important note` → agent sees `!important note`
- **Leading space**: ` !important note` → bypasses interception and is sent as normal chat (the normal send path trims the leading space)

The composer highlights the `!<target>` token in terminal green when it will be intercepted.

### Multiline Commands

Use **Shift+Enter** to insert newlines in a bang command. The shell receives it as if you pasted multiline text.

### Attachments

Bang commands **cannot** have attachments. If you try to submit `!` with files attached, the draft and files stay in place and you'll see: "Shell commands can't carry attachments."

### No Shell Access

If the current agent doesn't declare any shell types (no `terminals:` block in its spec) and has no live user shells, submitting `!` shows: "this agent has no shell access."

## Permissions

Only the **session owner** can run bang commands. The menu stays hidden for non-owners; submitting `!` shows: "Only the session owner can run shell commands."

## Example Workflow

1. Open a new shell: `!`
2. Set up your environment: `!u-abc123 cd ~/project && source venv/bin/activate`
3. Run a long command: `!u-abc123 pytest tests/`
4. Check results in the live terminal (right rail) while continuing to chat

## Differences from Regular Terminal Usage

- **No LLM context**: Command output is NOT automatically sent to the agent. The agent can still read shell panes via its existing tools if needed.
- **Owner-only**: Unlike typing directly in a shared terminal, only the session owner can submit bang commands.
- **Receipts**: Every command-bearing submission creates an audit trail in the conversation; create/focus-only forms do not create command receipts.
- **Client-side interception**: Bang commands never become agent messages; they're handled before reaching the agent.

## Technical Notes

- Commands are passed verbatim to the shell via `tmux send-keys -l` — Omnigent does not interpret quotes, `$VAR`, pipes, or `&&`.
- Your shell (bash, zsh, fish) handles all syntax and expansion.
- Concurrent sends to the same shell are serialized automatically.
