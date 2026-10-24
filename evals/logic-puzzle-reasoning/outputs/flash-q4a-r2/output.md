# Logic Puzzle: Five Researchers at Five Conferences

## Step 0: List What We Know Directly

| Clue | Direct Fact |
|------|------------|
| 1 | Ada → NeurIPS, Computer Vision |
| 8 | Ben → August |
| 11 | Clara → NLP |
| 2 + 11 | Clara → NLP → **June** |
| 12 | ICML → March |
| 5 | Robotics → ICLR |
| 7 | Theory → ACL |
| 6 | Eli → January **or** October |
| 3 | Ben ∉ {ACL, EMNLP}; Ben ∉ {Theory, Robotics} |
| 4 | Clara's month < Dana's month |
| 9 | EMNLP ≠ January |
| 10 | Dana ≠ Computer Vision |

---

## Step 1: Pin Down Ben's Conference

Ben cannot be at ACL or EMNLP (Clue 3) or NeurIPS (Ada's, Clue 1).
Remaining options: **ICML** or **ICLR**.

ICML is in March (Clue 12), but Ben is in August (Clue 8). **Ben ≠ ICML.**

> **∴ Ben → ICLR**

## Step 2: Flag the First Contradiction

Clue 5 says ICLR → Robotics. So Ben → Robotics.
But Clue 3 explicitly says Ben is **not** on Robotics.

> ⚠️ **CONTRADICTION #1.** The conference assignment for Ben is *forced* (ICLR), but that forces the subfield to Robotics, which Clue 3 forbids. No alternative assignment exists. I will proceed with the forced assignment (Ben → ICLR → Robotics) and note this violation.

## Step 3: Assign Clara's Conference

Clara's remaining conference options (after removing NeurIPS for Ada, ICLR for Ben): **ACL, EMNLP, ICML**.

- Clara is in **June** (Step 0). ICML is in March. → **Clara ≠ ICML**.
- Clara is on **NLP**. Theory → ACL (Clue 7). NLP ≠ Theory. → **Clara ≠ ACL**.

> **∴ Clara → EMNLP** (and June ≠ January, so Clue 9 is satisfied ✓)

## Step 4: Assign Dana's and Eli's Conferences

Remaining conferences for Dana and Eli: **ACL** and **ICML**.

- ICML → March. Eli must be in January or October (Clue 6). March ∉ {Jan, Oct}. → **Eli ≠ ICML**.

> **∴ Eli → ACL → Theory** (via Clue 7)
> **∴ Dana → ICML → March**

## Step 5: Flag the Second Contradiction

Clue 4: Clara's month must come **before** Dana's month.
Clara → **June** (6). Dana → **March** (3).
June comes **after** March, not before.

> ⚠️ **CONTRADICTION #2.** Clue 4 is violated. The only way Dana could be after June is August or October, but August is Ben's and October would require Dana at ACL or NeurIPS—both already taken. No alternative assignment exists.

## Step 6: Assign Remaining Subfields

Assigned so far: Ada → CV, Ben → Robotics, Clara → NLP, Eli → Theory.
Remaining subfield: **Reinforcement Learning** → **Dana**.

Check Clue 10: Dana ≠ CV. Dana → RL. ✓

## Step 7: Assign Remaining Months

Months assigned: Clara → June, Ben → August, Dana → March.
Remaining: **January** and **October** for **Ada** and **Eli**.

Clue 6 says Eli ∈ {January, October} — both remain available.
No other clue distinguishes January from October for either person.

> The puzzle does **not** uniquely determine which of Ada/Eli gets January vs. October.

---

## Final Table (Two Valid Solutions Differing Only in Ada/Eli Month)

| Researcher | Conference | Month | Subfield |
|:-----------|:-----------|:------|:---------|
| **Ada** | NeurIPS | **January / October** | Computer Vision |
| **Ben** | ICLR | August | Robotics † |
| **Clara** | EMNLP | June | NLP |
| **Dana** | ICML | March | Reinforcement Learning |
| **Eli** | ACL | **October / January** | Theory |

† Violates Clue 3's "not Robotics" (see Contradiction #1).

---

## Verification Against Every Clue

| # | Clue | Check |
|---|------|-------|
| 1 | Ada → NeurIPS, CV | ✅ |
| 2 | NLP in June | ✅ (Clara/NLP/June) |
| 3 | Ben ∉ {ACL, EMNLP}; Ben ∉ {Theory, Robotics} | Ben ∉ ACL/EMNLP ✅; **Ben ∉ Robotics ❌** |
| 4 | Clara's month < Dana's month | **June > March ❌** |
| 5 | Robotics → ICLR | ✅ (Ben/ICLR/Robotics) |
| 6 | Eli → Jan or Oct | ✅ |
| 7 | Theory → ACL | ✅ (Eli/ACL/Theory) |
| 8 | Ben → August | ✅ |
| 9 | EMNLP ≠ January | ✅ (EMNLP = June) |
| 10 | Dana ≠ CV | ✅ (Dana = RL) |
| 11 | Clara → NLP | ✅ |
| 12 | ICML → March | ✅ |

**10 of 12 clues satisfied. Clues 3 (partially) and 4 are violated.**

---

## Conclusion

> **The puzzle, as stated, has no fully consistent solution.** Two clues are mutually incompatible with the rest of the constraint system:
>
> 1. **Clue 3 vs. Clues 1, 8, 12, 5:** Ben's conference is *forced* to be ICLR, which *forces* his subfield to be Robotics, but Clue 3 forbids Robotics.
> 2. **Clue 4 vs. the forced chain:** Dana is *forced* to ICML/March while Clara is *forced* to June, making Clara's month *after* Dana's, the reverse of what Clue 4 demands.
>
> The table above is the **unique assignment** (up to the Ada/Eli month swap) that satisfies the maximum number of clues. If Clue 3 dropped "or Robotics" and Clue 4 were reversed ("Dana's month comes before Clara's"), the puzzle would be fully consistent and the table would be the answer.