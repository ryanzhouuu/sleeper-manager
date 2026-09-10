# Sleeper Fantasy Basketball: Lock-In Mode

> Source: [Sleeper Support — Lock-In Mode Details](https://support.sleeper.com/en/articles/6522833-lock-in-mode-details)
>
> Article updated February 18, 2026. This summary was last reviewed September 10, 2026.

## Overview

In Lock-In Mode, only **one NBA game per player per week** counts toward your fantasy matchup score.

After a player completes a game, you can either:

1. **Lock in** that game's fantasy score.
2. **Pass** and wait for a later game that week.

## How locking in works

- You can lock in a completed game only if the player was in your starting lineup for that game.
- You must lock in the score **before the player's next game begins**.
- Once you submit a lock-in:
  - The selected game's score is final for that player for the week.
  - Its points count toward your matchup.
  - You cannot replace it with a score from a later game that week.
  - You cannot move the player to another roster position.
- A submitted lock-in cannot be undone or changed, even if the player's next game has not started.

## Lineup and roster rules

### Moving a player between starting positions

Before locking in, you may move a player between eligible **starting** roster positions without losing the ability to lock in the completed game's score. Sleeper's published rule does not impose a game-in-progress freeze on this starter-to-starter movement.

After locking in, the player cannot be moved to another position.

### Moving a bench player after a game

You cannot move a player from the bench into a starting position after their game and then lock in that game's score. The player must have been in your starting lineup when the game was played.

Moving an eligible starter to the bench forfeits that player-game performance's Lock-In eligibility. Returning the player to a starting position later does not restore eligibility for that performance.

### Active and locked players are different states

An active starter has begun a qualifying game but has not yet accepted a score. The player remains movable between eligible starting positions. Moving the player to the bench makes that player-game performance ineligible for later Lock-In evaluation.

A locked player has an accepted score. Their player, game, score, and starter slot are fixed for the remainder of the fantasy week.

An active starter must not be treated as locked, but a lineup update must not silently omit or bench them. A recommendation may move an active starter to the bench only as an explicit decision to forfeit that player-game performance.

### Advisor and replay requirements

- Capture Lock-In eligibility from the lineup that existed when each game began.
- Preserve every active starter in subsequent target lineups unless the policy explicitly chooses to forfeit that player-game performance.
- Allow an active starter to move between eligible starting positions and retain the original player-game eligibility record.
- Record an active starter's move to the bench as an irreversible loss of eligibility for that player-game performance.
- Keep accepted Lock-In slots immovable.
- Treat a target lineup as complete state: applying a partial target must not implicitly bench omitted active starters.
- Validate generated moves against these states before recommending or replaying them.

### Adding a free agent or waiver player

You cannot add a player and receive points from a game they played before joining your roster. You may use the player for games going forward, provided they are in your starting lineup when those games are played.

## Trades

A player's new fantasy team can earn points only from games played after the trade. Scores from games before the trade stay with the original fantasy team.

**Example:** If Team A locks in James Harden's score and then trades him to Team B, that week's locked-in points remain with Team A.

## If you do not lock in a game

If you never lock in a score for a player, Sleeper automatically uses the fantasy points from that player's NBA team's **final game of the week**.

- If the player does not play in that final game, the result can be zero points.
- Sleeper does not fall back to an earlier game the player played.
- To avoid a zero, replace a player who will not play with another eligible player when possible.

## All-Star weeks

Sleeper keeps the All-Star weeks as separate fantasy matchups. Commissioners cannot merge them, and Sleeper does not plan to support merging them.

If a league does not want one or both weeks to count, the commissioner can set the scores for the affected week to zero. Every matchup that week will then end in a `0-0-1` tie.

## Quick checklist

Before locking in a player's score, confirm that:

- The player was in a starting position when the game was played.
- The player has not subsequently been moved to the bench.
- The player's next game has not started.
- You are satisfied with the score, because the decision is permanent.
- The player is in the roster position where you want them to remain.
