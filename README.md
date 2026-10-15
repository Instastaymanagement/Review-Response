# Review Response

Every day at **13:17 UTC** (two hours after the [Review-Automation](https://github.com/Instastaymanagement/Review-Automation) sync), this workflow replies publicly to recent 5-star guest reviews.

1. **Find:** reads the *All Reviews Log* boards in **USA Automated (Trial)** and **Canada Automated(Trial)** on monday.com. It picks items where:
   - *Overall Rating* is 5,
   - *Host Responded* is unchecked,
   - *Review Date* is within the last 30 days.
2. **Check:** looks up each review in Hospitable and skips it if:
   - it already has a reply (the monday item just gets *Host Responded* ticked), or
   - Hospitable says the platform's reply window has closed.
3. **Draft:** Claude writes a short, personal reply in the host's voice. It works from the guest's first name, the property, and the **public** review text. It is also shown a few of the host's own past replies as style examples. **Private feedback is never sent to Claude**, so it can't end up in a public reply.
4. **Publish:** posts the reply through Hospitable, ticks *Host Responded* on the monday item, and adds the posted text as an update on that item.

Each run posts at most 40 replies, newest reviews first. Anything left over is picked up the next day.

## Setup

Add these repository secrets (**Settings → Secrets and variables → Actions**):

| Secret | Notes |
|---|---|
| `HOSPITABLE_API_TOKEN` | Hospitable personal access token. It needs **write** access so it can post review replies. |
| `MONDAY_API_TOKEN` | monday.com personal API token |
| `ANTHROPIC_API_KEY` | Claude API key from console.anthropic.com |
| `ANTHROPIC_WORKSPACE_ID` | Only needed if the API key isn't scoped to a workspace. Copy the workspace ID from Console → Settings → Workspaces. |

## Running

- **Scheduled:** runs daily and **posts for real**.
- **Manually:** Actions → *Reply to 5-star reviews* → Run workflow. **dry_run** is ticked by default; it drafts replies and shows them on the run page without posting anything.
- **Settings:** change `MAX_AGE_DAYS` (default 30) and `MAX_PER_RUN` (default 40) in `.github/workflows/respond-reviews.yml`.

## Tests

```
pip install -r requirements.txt
python -m unittest discover -s tests
```
