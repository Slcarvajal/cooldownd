# Avatar sprites

Put your avatar's strips here, one PNG per outfit: `casual.png`, plus one named like an activity in config `life.activities` (`gym.png`, `run.png`…). The game shows the outfit of the last activity you did today, and `casual` otherwise. With no strips, a drawn pixel avatar stands in.

Make them from 4×4 sprite sheets (rows: front, facing left, facing right, back; columns: stand, step, step, the outfit's move; the last cell is a celebration):

```bash
uv run admin/life/make_sprites.py casual=sheet-casual.png gym=sheet-gym.png run=sheet-run.png
```

A black background is cut away. Every frame lands on one canvas with the feet on one line, so the animation doesn't jitter. The strips are git-ignored: they're your face.
