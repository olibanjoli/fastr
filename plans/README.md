# plans

Hand-written training plans as CSV, one row per week, loaded with:

```powershell
uv run fastr plan import plans/my-plan.csv
uv run fastr report --plan-source claude_plan
```

Columns: `week, start` (the Monday), `phase, total_km`, then for each day `mon tue wed thu fri sat sun`
a `<day>_km` number and a `<day>` text, and `notes`. The text starts with a session code:

| Code | Meaning |
|---|---|
| `E` | easy run |
| `T` | threshold session |
| `I` | intervals |
| `MP` | marathon-pace session |
| `L` | long run |
| `RACE` | race or time trial |
| `SHAKE` | shakeout jog |
| anything else with 0 km | strength, rest, cross-training |

`example-plan.csv` shows the format. Your own plans in this folder are git-ignored; only the
README and the example are tracked.
