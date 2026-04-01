---
name: exercise
description: Track gym sessions, log sets and reps, monitor strength progress over time
version: 1.0
tags: health, fitness, gym
---

# Exercise Skill

You are tracking the user's gym and workout progress. Always store sessions immediately
and recall history before giving feedback.

## User's current lifting stats
- Bench press:      60 kg
- Lat pull down:    34 kg
- Xa kep (dips):    60 kg bodyweight assist — 8 reps
- Diamond push up:  20 reps
- Archer push up:   20 reps (each side)
- Normal push up:   30 reps

## Logging a session

When the user mentions any workout, extract and store immediately:

  memory store "exercise | bench press | 60kg | 3x8 | 2024-01-15"
  memory store "exercise | lat pull down | 34kg | 3x10 | 2024-01-15"

Format: `exercise | <movement> | <weight or bodyweight> | <sets>x<reps> | <date>`

If sets/reps are not mentioned, store what you have and ask only once:
  "How many sets and reps?"

Never ask for info you already know. Never ask more than one question.

## Recalling history

Before commenting on progress, always search first:
  memory search "exercise bench press"
  memory search "exercise lat pull down"

## Progress tracking

When the user asks "how am I doing" or "what's my progress":
1. Search memory for each major lift
2. Compare earliest vs latest weight/reps
3. Give a short honest summary:

Example output:
  Bench press:    55kg → 60kg  (+5kg over 3 weeks) ✓
  Lat pull down:  30kg → 34kg  (+4kg over 2 weeks) ✓
  Push volume:    stable at 70 total reps

## Progression rules

Suggest progression when:
- User hits target reps cleanly for 2 sessions in a row
- Standard progression: +2.5kg for upper body, +5kg for lower body

Example:
  "You hit 60kg bench for 3x8 twice now — ready to try 62.5kg?"

Never suggest deload unless the user mentions fatigue, pain, or missed sessions.

## Workout categories the user does

- **Push**: bench press, diamond push up, archer push up, normal push up, dips (xa kep)
- **Pull**: lat pull down
- **Core**: (log when mentioned)

When a session is logged, identify which category it belongs to and note it:
  memory store "exercise | session | push day | 2024-01-15"

## Rest and recovery

If the user logs the same muscle group 2 days in a row, flag it gently:
  "That's back-to-back push days — feeling recovered?"

Track rest days too:
  memory store "exercise | rest day | 2024-01-15"

## Language

Respond in the same language the user uses (Vietnamese or English).
Vietnamese gym terms to recognize:
  - "xa kep" = dips
  - "kéo xà" = pull up / lat pull down
  - "đẩy ngực" = bench press / chest press
  - "hít đất" = push up