# Skills

## Publish

**Trigger:** When user says "publish", "deploy", or similar.

**Description:** Commits current changes and deploys the data_log repo to all Pi Zero 2W devices via SSH git pull.

**Steps:**

1. Use `AskUserQuestion` to confirm with the user:
   - Question: "Would you like me to commit your changes and publish to the Pi Zero 2W devices?"
   - Options: "Yes" and "No"

2. If user selects "Yes":
   - Run `git status` to check for uncommitted changes
   - If there are changes, create a commit with an appropriate message
   - Push to remote (user may need to do this manually if SSH keys aren't configured)
   - Run `./publish.sh` to SSH to pz2w1-4 and execute `git pull` on each

3. If user selects "No":
   - Acknowledge and do nothing

**Notes:**
- The `publish.sh` script handles SSH connections to: pz2w1, pz2w2, pz2w3, pz2w4
- All devices pull from the same path: `/home/nkrueger/dev/data_log`
- Use `--reinstall` flag with publish.sh if venv needs to be rebuilt on remotes
