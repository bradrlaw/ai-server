# Solving the Five-Researcher Logic Puzzle

## Step 1: Extract All Direct Facts

From the clues, I can immediately record:

| Clue | Direct Fact |
|------|------------|
| 1 | **Ada → NeurIPS, Computer Vision** |
| 2 | **NLP → June** |
| 3 | **Ben ≠ ACL, Ben ≠ EMNLP, Ben ≠ Theory, Ben ≠ Robotics** |
| 4 | **Clara's month < Dana's month** |
| 5 | **Robotics → ICLR** |
| 6 | **Eli ∈ {January, October}** |
| 7 | **Theory → ACL** |
| 8 | **Ben → August** |
| 9 | **EMNLP ≠ January** |
| 10 | **Dana ≠ Computer Vision** |
| 11 | **Clara → NLP** |
| 12 | **ICML → March** |

## Step 2: Deduce Clara's Month

From Clue 11, Clara's subfield is **NLP**. From Clue 2, the NLP paper was presented in **June**.

> **Clara → June**

## Step 3: Pin Down Ben's Conference

Ben's conference cannot be:
- **ACL** (Clue 3), **EMNLP** (Clue 3), **NeurIPS** (occupied by Ada, Clue 1).

So Ben ∈ {**ICML, ICLR**}.

From Clue 8, Ben is in **August**. From Clue 12, ICML is in **March**. Since August ≠ March, Ben ≠ ICML.

> **Ben → ICLR**

## Step 4: Assign Ben's Subfield — ⚠️ Contradiction Detected

From Clue 5, the Robotics paper was presented at ICLR. Since Ben is at ICLR:

> **Ben → Robotics**

But Clue 3 explicitly states **Ben's paper was NOT on Robotics**.

**Every step above is forced by the clues; there is no branching.** This is a genuine contradiction in the puzzle as written. The most likely intended wording for Clue 3 is *"…not on Theory or **Computer Vision**"* (which is automatically satisfied since Ada holds CV). I will proceed with that correction.

## Step 5: Continue with the Corrected Clue 3

Ben: **ICLR, August, Robotics** (satisfies ≠ ACL ✓, ≠ EMNLP ✓, ≠ Theory ✓, ≠ CV ✓).

## Step 6: Place Clara's Conference

Clara (NLP, June) needs a conference. Remaining options: {ACL, EMNLP, ICML} (NeurIPS = Ada, ICLR = Ben).

- **ACL** → Theory (Clue 7) ≠ NLP. ✗
- **ICML** → March (Clue 12) ≠ June. ✗
- **EMNLP** → no conflict. ✓

> **Clara → EMNLP, June, NLP**

Check Clue 9: EMNLP ≠ January. EMNLP = June. ✓

## Step 7: Determine Dana's Month

Clue 4: Clara's month (June) < Dana's month. So Dana ∈ {August, October}.
August is taken by Ben, so:

> **Dana → October**

## Step 8: Determine Ada's and Eli's Months and Conferences

Remaining months: **January, March**. Remaining conferences: **ACL, ICML**.

- ICML = March (Clue 12). Ada is at NeurIPS (Clue 1), so Ada ≠ ICML, hence Ada ≠ March.
- Therefore **Ada → January** and **Eli → March → ICML**.

> **Ada → NeurIPS, January, Computer Vision**
> **Eli → ICML, March, ?**

⚠️ **Second contradiction:** Clue 6 says Eli presented in January or October. January is Ada's; October is Dana's. Eli is forced into March. The most likely intended wording is *"Eli presented in either **March** or October."* I proceed with that correction.

## Step 9: Assign Remaining Conferences

Dana's conference = the one left after Ada (NeurIPS), Ben (ICLR), Clara (EMNLP), Eli (ICML):

> **Dana → ACL**

## Step 10: Assign Remaining Subfields

- **Theory → ACL** (Clue 7) → **Dana → Theory** ✓ (also satisfies Dana ≠ CV, Clue 10)
- Remaining subfield for Eli: **Reinforcement Learning**

> **Dana → ACL, October, Theory**
> **Eli → ICML, March, Reinforcement Learning**

## Step 11: Final Table

| Researcher | Conference | Month | Subfield |
|------------|------------|-------|----------|
| **Ada** | NeurIPS | January | Computer Vision |
| **Ben** | ICLR | August | Robotics |
| **Clara** | EMNLP | June | NLP |
| **Dana** | ACL | October | Theory |
| **Eli** | ICML | March | Reinforcement Learning |

## Step 12: Verify Every Clue

| # | Clue | Check |
|---|------|-------|
| 1 | Ada → NeurIPS, CV | ✅ |
| 2 | NLP in June | Clara, NLP, June ✅ |
| 3 | Ben ≠ ACL, ≠ EMNLP, ≠ Theory, ≠ **CV** *(corrected)* | ICLR, Robotics ✅ |
| 4 | Clara (June) < Dana (October) | June < October ✅ |
| 5 | Robotics → ICLR | Ben, Robotics, ICLR ✅ |
| 6 | Eli ∈ {**March**, October} *(corrected)* | Eli = March ✅ |
| 7 | Theory → ACL | Dana, Theory, ACL ✅ |
| 8 | Ben → August | ✅ |
| 9 | EMNLP ≠ January | EMNLP = June ✅ |
| 10 | Dana ≠ CV | Dana = Theory ✅ |
| 11 | Clara → NLP | ✅ |
| 12 | ICML → March | Eli, ICML, March ✅ |

## Step 13: Note on Contradictions

> ⚠️ **The puzzle as literally written contains two contradictions:**
>
> 1. **Clue 3 vs. Clues 1+5+8+12:** The chain *Ben ≠ ACL/EMNLP/NeurIPS → Ben = ICLR → Robotics* directly contradicts Clue 3's "Ben ≠ Robotics." The most natural fix is reading Clue 3 as "not on Theory or **Computer Vision**."
>
> 2. **Clue 6 vs. the forced month assignment:** Eli is mathematically forced into March, but Clue 6 restricts Eli to {January, October}. The most natural fix is reading Clue 6 as "Eli presented in either **March** or October."
>
> With those two single-word corrections, the solution above is **unique** and satisfies all twelve clues. Without them, the constraint set is **unsatisfiable**.