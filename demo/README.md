# Presentation Demo

This folder contains a small scripted Git repository setup for the presentation
demo. It creates a separate demo repository with one logged SQLite database and
four transactions on each branch.

From the project root:

```sh
demo/setup_demo_repo.sh --force
cd /private/tmp/sqlite-reconcile-demo
git merge remote || git sqlite-reconcile
```

The demo covers:

- branch-local replay warning from unsafe nondeterminism
- write-read conflict
- integrity conflict
- write-write conflict
- multi-statement transactions
- edit, accept, and delete resolution actions

Suggested live resolutions:

| Conflict | Action |
| --- | --- |
| `L1` nondeterminism | `edit L1.1`, replace `random()` with `'fixed-demo-token'` |
| `L2` vs `R4` write-read | press Enter to accept the reviewable conflict |
| `L3` vs `R2` integrity | `edit R2.1`, change `shared@example.com` to `dave@example.com` |
| `L4` vs `R3` write-write | `edit R3.1` to the combined `+30` update, then `delete L4.1` |

Exact replacement SQL:

```sql
UPDATE users SET token = 'fixed-demo-token' WHERE id IN (1, 2);
```

```sql
INSERT INTO users(id, name, email, token)
VALUES (4, 'Dave', 'dave@example.com', 'remote-dave');
```

```sql
UPDATE accounts SET balance = balance + 30 WHERE id = 1;
```

After the merge:

```sh
sqlite3 app.db < check_state.sql
```
