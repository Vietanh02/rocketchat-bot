# Folders to import into the knowledge base

Copy a folder here (for example a Confluence space exported with
`confluence-markdown-exporter`), then import it from the portal's
**Knowledge base → Import a folder → From the server**. Each folder becomes a
collection; importing it again syncs it (changed pages replaced, removed pages
removed). The folder is mounted read-only in the backend at `/app/kb_import`.

    rsync -a ./MYSPACE/ server:/path/to/rocketchat_bot/kb_import/MYSPACE/

Everything in this folder except this README is ignored by git.
