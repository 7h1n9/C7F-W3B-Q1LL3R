---
name: muteki-blackboard
description: >
  Shared team blackboard for a CTF/pentest solver swarm. ALWAYS use this skill
  whenever you are solving a challenge as part of a team - before starting any
  new direction (check what teammates already ruled out), when you confirm a fact
  (write it so teammates benefit), and when you hit a dead end (mark it so nobody
  retries it). Use it whenever a task mentions a "blackboard", "shared notes",
  "teammates", "the board", "what others found", "intents", or coordinating with
  other agents. Reading the board first saves you from repeating work others
  already proved impossible.
---

# Team Blackboard

You are ONE worker in a swarm. Your teammates are other AI agents working the same
challenge. You do **not** talk to them directly - you coordinate through a shared
**blackboard** (a fact/intent graph). The `blackboard.py` script in this skill is
your interface to it.

## When to use it (this is the important part)

Run these at the RIGHT moments - not constantly, not never:

1. **Before you start a direction** - check what's already been ruled out:
   ```
   python3 blackboard.py read-deadends
   python3 blackboard.py read-review
   ```
   If your idea is already on the dead-end list, suppressed by Review-Arbiter, or
   depends on a challenged fact, pick a different angle or prove/disprove the
   challenged fact first.

2. **When you're stuck or switching angles** - see what teammates confirmed:
   ```
   python3 blackboard.py read-facts
   python3 blackboard.py read-routes
   python3 blackboard.py read-branches
   ```
   A fact someone else verified (a leaked cred, a service version, a decoded
   intermediate) may be exactly the stepping stone you need. On a **multi-flag**
   challenge, also check which flags are already recovered:
   ```
   python3 blackboard.py read-flags
   ```

3. **The moment you CONFIRM something in real output** - write it back:
   ```
   python3 blackboard.py write-fact "admin:admin logs in at /login (302 -> /dashboard)" --verified
   ```
   Use `--verified` only for things you saw in REAL command output. Drop it for a
   strong hypothesis you haven't proven. Keep facts short and objective.

4. **When you rule a direction out** - mark it dead so nobody retries:
   ```
   python3 blackboard.py mark-deadend "no SQLi on /search - all params parameterized"
   ```

5. **If you were assigned to pick up open work** - claim an intent first:
   ```
   python3 blackboard.py list-intents
   python3 blackboard.py claim I3
   ```
   `claim` prints `WON` (it's yours) or `LOST` (a teammate beat you - pick another).

6. **Before destructive / exclusive work** (remote RCE, a reverse-shell listener, a
   relay, an exclusive shell, a rate-limited account) - claim the RESOURCE so two
   workers don't collide:
   ```
   python3 blackboard.py read-resource-locks
   python3 blackboard.py claim-resource "destructive:tcp:445@172.22.11.45" --risk-class destructive
   ...do the work...
   python3 blackboard.py release-resource "destructive:tcp:445@172.22.11.45"
   ```

7. **Before expensive / easy-to-duplicate activity** (nmap of the same range, a
   brute-force of the same username, downloading the same big attachment):
   ```
   python3 blackboard.py list-activities
   python3 blackboard.py claim-activity "nmap:8.130.96.176"
   ```
   `WON` = go ahead. `LOST` = a teammate is already doing it, pick another angle.

8. **Respect operator / coordinator directives** (must-follow guidance):
   ```
   python3 blackboard.py read-directives
   ```

## What never goes on the board

- Raw HTTP responses, cookies, tokens, secrets, or challenge ground truth that
  could break the challenge.
- Huge logs. Write short objective facts, keep evidence references compact.

## Environment

The coordinator sets `MUTEKI_BLACKBOARD_DB` to the run's `shared_graph.db`.
The worker's id is `MUTEKI_WORKER_ID`; when a worker executes inside a claimed
intent, `MUTEKI_INTENT_ID` links written facts back to that intent
(`intent_products`).
