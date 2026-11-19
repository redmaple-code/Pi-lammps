# Sanitization Notes (2026-09-21)

This package was sanitized from an internal deployment for sharing:

- **Removed**: knowledge/experience base (atoms, scenarios, SOP, distilled knowledge),
  session archives, runtime credential configs (`cloud_client.json`), logs, backups,
  third-party copyrighted PDFs, `node_modules`, `.git`.
- **Redacted**: local absolute paths -> `/opt/mdagent/...` placeholders; usernames ->
  generic (`user`); passwords/tokens -> `<REDACTED-*>`; server IP -> `<SERVER-IP>`;
  research project/system codenames -> pseudonyms (e.g. `SYS-A`, `PRJ`, `RUN1500`);
  personal npm scope / GitHub handles (renamed to `@vendor` / `<UPSTREAM-CORPUS-REPO>`).
  Note: lockfiles were text-edited for the scope
  rename — run `pnpm install` to re-resolve rather than trusting them verbatim.
- **Retained third-party attributions** (not personal info, kept for license
  compliance): `tt-a1i/archify` MIT copyright in `tools/archify/`, `@deepseek-ai/*`
  upstream packages.
- **Included third-party data**: `external_corpus/` (LAMMPS/GROMACS forum Q&A on MD
  errors) is licensed **CC BY-NC-SA 4.0** with per-record upstream URL/author
  attribution preserved — see `external_corpus/README.md`. Non-commercial use only.
- Corpus content itself (forum posts by third parties) is unmodified.
