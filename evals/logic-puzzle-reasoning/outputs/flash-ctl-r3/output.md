# Logic Puzzle: Five Researchers, Five Conferences

## Step 1: Extract All Direct Facts

| Clue | Fact |
|------|------|
| 1 | Ada → NeurIPS, Computer Vision |
| 2 | NLP → June |
| 3 | Ben ≠ ACL, ≠ EMNLP; Ben ≠ Theory, ≠ Robotics |
| 4 | Clara's month < Dana's month |
| 5 | Robotics → ICLR |
| 6 | Eli ∈ {January, October} |
| 7 | Theory → ACL |
| 8 | Ben → August |
| 9 | EMNLP ≠ January |
| 10 | Dana ≠ Computer Vision |
| 11 | Clara → NLP |
| 12 | ICML → March |

Combining Clues 2 + 11: **Clara → NLP → June.**

---

## Step 2: Pin Down the Months

- **Clara = June** (Clues 2, 11)
- **Ben = August** (Clue 8)
- **Clara < Dana** (Clue 4): months after June are August and October. August is Ben's. → **Dana = October**
- **Eli ∈ {Jan, Oct}** (Clue 6): October is Dana's. → **Eli = January**
- Only month left → **Ada = March**

| Researcher | Month |
|-----------|-------|
| Ada | March |
| Ben | August |
| Clara | June |
| Dana | October |
| Eli | January |

---

## Step 3: Pin Down the Conferences

- **Ada = NeurIPS** (Clue 1)
- Ben ≠ ACL, ≠ EMNLP (Clue 3), ≠ NeurIPS (Ada has it) → **Ben ∈ {ICML, ICLR}**
- Clue 5 says the Robotics paper is at ICLR. Clue 3 says Ben's paper is **not** Robotics. Since the person at ICLR *must* work on Robotics (bijection), **Ben ≠ ICLR**.
- → **Ben = ICML**

Now Clue 12 says **ICML = March**. But we just proved **Ben = ICML**, so Ben would be in March. Yet Clue 8 says **Ben = August**.

> ⚠️ **CONTRADICTION DETECTED**
> Ben is forced to ICML (by Clues 1, 3, 5).
> ICML is forced to March (Clue 12).
> Ben is forced to August (Clue 8).
> March ≠ August. These three statements cannot all hold.

A second, independent contradiction confirms it: Ada's month is forced to March (Step 2), ICML is forced to March (Clue 12), so Ada would be at ICML — but Clue 1 says Ada is at **NeurIPS**.

---

## Step 4: Resolve Toward the Intended Solution

The contradiction lives entirely in the clash between **Ben = ICML**, **ICML = March**, and **Ben = August**. The most natural single-correction fix is to read Clue 12 as placing ICML in **August** (aligning with Ben), freeing March for Ada. I proceed with that single adjustment and solve the rest.

### Conferences (remaining)

- Ada = NeurIPS, Ben = ICML, Clara = ?
- Clara ≠ ACL (ACL = Theory per Clue 7; Clara = NLP), ≠ ICLR (ICLR = Robotics per Clue 5; Clara = NLP), ≠ NeurIPS, ≠ ICML → **Clara = EMNLP**
- Check Clue 9: EMNLP ≠ January. EMNLP = June. ✓
- Remaining: **Dana and Eli get ACL and ICLR.**

### Subfields

- Ada = CV, Clara = NLP.
- Ben ≠ Theory, ≠ Robotics (Clue 3) → **Ben = Reinforcement Learning**.
- Remaining subfields for Dana & Eli: **Theory** and **Robotics**.
- Clue 7: Theory → ACL. Clue 5: Robotics → ICLR.
- Clue 10: Dana ≠ CV (already satisfied either way).
- Two sub-assignments remain open; I'll resolve them below.

### Months (with ICML now in August)

- Ada = March (only slot left after assigning Ben = August, Clara = June, Dana = October, Eli = January).
- NeurIPS = March (Ada's conference). No conflict since ICML is now in August.

### Resolving Dana vs. Eli

With no further clue to separate them, I test both branches:

| Branch | Dana | Eli |
|--------|------|-----|
| A | ACL, October, Theory | ICLR, January, Robotics |
| B | ICLR, October, Robotics | ACL, January, Theory |

Both satisfy every remaining clue (including Clue 10). The puzzle, even after the fix, has **two valid solutions** for the Dana/Eli pair. I present Branch A as the primary answer (it follows the natural reading order of Clues 7 → 5), and note Branch B as the alternative.

---

## Step 5: Final Answer (Primary Solution)

| Researcher | Conference | Month | Subfield |
|:----------:|:----------:|:-----:|:--------:|
| **Ada** | NeurIPS | March | Computer Vision |
| **Ben** | ICML | August | Reinforcement Learning |
| **Clara** | EMNLP | June | NLP |
| **Dana** | ACL | October | Theory |
| **Eli** | ICLR | January | Robotics |

*(Alternative: swap Dana ↔ Eli's conference and subfield.)*

---

## Step 6: Verify Every Clue

| # | Clue | Check | Result |
|---|------|-------|--------|
| 1 | Ada at NeurIPS, CV | Ada = NeurIPS, CV | ✅ |
| 2 | NLP presented in June | Clara = NLP = June | ✅ |
| 3 | Ben ≠ ACL, ≠ EMNLP; ≠ Theory, ≠ Robotics | Ben = ICML, RL | ✅ |
| 4 | Clara's month before Dana's | June < October | ✅ |
| 5 | Robotics at ICLR | Eli = Robotics = ICLR | ✅ |
| 6 | Eli in January or October | Eli = January | ✅ |
| 7 | Theory at ACL | Dana = Theory = ACL | ✅ |
| 8 | Ben in August | Ben = August | ✅ |
| 9 | EMNLP ≠ January | EMNLP = June | ✅ |
| 10 | Dana ≠ CV | Dana = Theory | ✅ |
| 11 | Clara on NLP | Clara = NLP | ✅ |
| 12 | ICML in March | ICML = **August** | ⚠️ |

---

## Step 7: Explicit Statement on Consistency

> **The puzzle as written contains a contradiction.** Clues 1 + 3 + 5 force Ben to ICML; Clue 12 places ICML in March; Clue 8 places Ben in August. These three cannot be simultaneously true. The table above satisfies **11 of 12 clues**; the single violation is Clue 12 (ICML's month). Additionally, after resolving that contradiction, the Dana/Eli conference-subfield pair is **underdetermined** (two valid branches remain), so the puzzle does not have a unique solution even after the fix.