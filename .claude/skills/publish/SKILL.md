---
name: publish
description: Deploy code changes to Pi Zero 2W devices. Use when code is ready to test on hardware, user says "publish", "deploy", "let's test this", or a feature/fix is complete and needs hardware verification.
---

# Publish to Pi Devices

Deploy committed changes to the Pi Zero 2W sensor network devices.

## Steps

1. Run `git status` to check for uncommitted changes

2. If there are changes to commit:
   - Stage the changed files
   - Create a commit with an appropriate message

3. Run `git push` to push to GitHub
   - If push fails with "could not read Username", credentials aren't configured
   - Help user set up credentials:
     ```bash
     git config --global credential.helper store
     # Then create ~/.git-credentials with: https://<user>:<token>@github.com
     ```

4. Run `./publish.sh` to SSH to pz2w1-4 and execute `git pull` on each

## Notes

- Devices: pz2w1, pz2w2, pz2w3, pz2w4 (some may be offline)
- Use `./publish.sh --reinstall` if dependencies changed
- Always confirm with user before committing unless explicitly instructed
