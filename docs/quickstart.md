# Quickstart

## 1) Initialize Database
```bash
python3 scripts/migrate.py --db .archivist/archivist.db
```

## 2) Start STDIO Server
```bash
python3 -m archivist_mcp.stdio_server --db .archivist/archivist.db
```

## 3) Create Seed Project + User
```bash
python3 - <<'PY'
from archivist_mcp.db import connect
c = connect('.archivist/archivist.db')
c.execute("INSERT INTO projects(project_id,name) VALUES('proj-1','Project One')")
c.execute("INSERT INTO users(user_id,display_name) VALUES('user-1','User One')")
c.commit(); c.close()
PY
```

## 4) First Write + Search
```bash
printf '{"id":1,"tool":"create_entity","args":{"project_id":"proj-1","type":"Entity","title":"Parser","content":"Parses files","user_id":"user-1"}}\n' | python3 -m archivist_mcp.stdio_server --db .archivist/archivist.db
printf '{"id":2,"tool":"search_graph","args":{"project_id":"proj-1","query":"parser"}}\n' | python3 -m archivist_mcp.stdio_server --db .archivist/archivist.db
```

## 5) Launch WebUI
```bash
python3 -m archivist_mcp.webui_server --db .archivist/archivist.db --host 127.0.0.1 --port 8090
```
