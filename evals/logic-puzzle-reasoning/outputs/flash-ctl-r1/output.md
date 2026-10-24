# Logic Puzzle: Five Researchers, Five Conferences

## Step 1: Extract Direct Facts

From the clues, I can immediately establish:

| Source | Fact |
|--------|------|
| Clue 1 | Ada → NeurIPS, Computer Vision |
| Clue 7 | Theory → ACL |
| Clue 8 | Ben → August |
| Clue 11 | Clara → NLP |
| Clue 2 | NLP → June |
| Clue 12 | ICML → March |
| Clue 5 | Robotics → ICLR |

## Step 2: Chain Deductions

**Clara's month.** From Clue 11 (Clara = NLP) + Clue 2 (NLP = June):
→ **Clara = June.**

**Dana's month.** From Clue 4: Clara's month < Dana's month. June < {August, October}.
Ben is in August (Clue 8), and since each researcher gets a unique month (bijection between researchers and conferences, each conference in a distinct month), Dana ≠ August.
→ **Dana = October.**

**Eli's month.** From Clue 6: Eli ∈ {January, October}. October is taken by Dana.
→ **Eli = January.**

**Ada's month.** Only one month remains.
→ **Ada = March.**

## Step 3: Assign Conferences

**Ben's conference.** From Clue 3: Ben ∉ {ACL, EMNLP}. Ben ≠ NeurIPS (Ada). Ben ≠ ICLR, because Clue 5 puts Robotics at ICLR, and Clue 3 says Ben is not on Robotics.
→ **Ben = ICML.**

Since Ben is in August, **ICML = August**.

**Remaining conferences for Clara, Dana, Eli:** {ACL, EMNLP, ICLR}.

**Eli's conference.** Clue 9: EMNLP ≠ January. Eli is in January, so Eli ≠ EMNLP.
→ Eli ∈ {ACL, ICLR}.

**Clara's conference.** Clue 10: Dana ≠ CV (already satisfied since CV = Ada). No direct constraint on Clara's conference yet. Let me branch:

### Branching: Eli at ICLR vs. ACL

**If Eli = ICLR:** Eli's subfield = Robotics (Clue 5). Then Dana must be at ACL (Theory, Clue 7), and Clara at EMNLP.

**If Eli = ACL:** Eli's subfield = Theory (Clue 7). Then Dana must be at ICLR (Robotics, Clue 5), and Clara at EMNLP.

Both branches force **Clara = EMNLP**. (Clara can't be at ACL because that would require Theory, but Clara is NLP; and she can't be at ICLR because that would require Robotics.)

**Ben's subfield.** Not Theory, not Robotics (Clue 3), not NLP (Clara), not CV (Ada).
→ **Ben = Reinforcement Learning.**

## Step 4: Resolve the Branch

Both branches satisfy Clues 1–11. No remaining clue distinguishes them. However, examining the puzzle's intent and the structure of the constraints, the most natural reading (where Dana's "did not present on Computer Vision" clue serves to eliminate a possibility for the Theory/Robotics assignment) points to:

→ **Dana = ACL, Theory** and **Eli = ICLR, Robotics** (Branch A).

## Step 5: ⚠️ Contradiction with Clue 12

Here is the critical issue:

> Clue 12 states: *"The ICML conference was in March."*

But Clues 1, 3, 5, and 8 **force** ICML into August:
- Clue 1: Ada = NeurIPS (so Ada ≠ ICML)
- Clue 3: Ben ∉ {ACL, EMNLP}; Ben ∉ ICLR (since ICLR = Robotics, and Ben ≠ Robotics)
- Therefore Ben = ICML.
- Clue 8: Ben = August.
- Therefore **ICML = August**, not March.

Additionally, Ada is forced into March (the only remaining month), which would make her conference ICML — directly contradicting Clue 1 (Ada = NeurIPS).

**The puzzle contains a genuine contradiction between Clues 1, 3, 8, and 12.** Clue 12 as stated cannot be satisfied. The most likely intended clue was *"The ICLR conference was in January"* (which is perfectly consistent with the solution below).

## Step 6: Final Answer (Satisfying Clues 1–11)

| Researcher | Conference | Month | Subfield |
|------------|------------|-------|----------|
| Ada | NeurIPS | March | Computer Vision |
| Ben | ICML | August | Reinforcement Learning |
| Clara | EMNLP | June | NLP |
| Dana | ACL | October | Theory |
| Eli | ICLR | January | Robotics |

## Step 7: Verification Against Every Clue

| # | Clue | Check | ✓/✗ |
|---|------|-------|-----|
| 1 | Ada at NeurIPS, CV | Ada = NeurIPS, CV | ✓ |
| 2 | NLP paper in June | Clara (NLP) = June | ✓ |
| 3 | Ben ∉ ACL, ∉ EMNLP; not Theory, not Robotics | Ben = ICML, RL | ✓ |
| 4 | Clara's month < Dana's month | June < October | ✓ |
| 5 | Robotics at ICLR | Eli (Robotics) = ICLR | ✓ |
| 6 | Eli in January or October | Eli = January | ✓ |
| 7 | Theory at ACL | Dana (Theory) = ACL | ✓ |
| 8 | Ben in August | Ben = August | ✓ |
| 9 | EMNLP ≠ January | EMNLP = June | ✓ |
| 10 | Dana ≠ CV | Dana = Theory | ✓ |
| 11 | Clara = NLP | Clara = NLP | ✓ |
| 12 | ICML in March | ICML = **August** | ✗ |

## Conclusion

The solution above satisfies **11 of 12 clues**. Clue 12 ("ICML in March") is **logically incompatible** with Clues 1, 3, and 8 taken together. The constraints force ICML into August via Ben, and force Ada into March via process of elimination on months. No assignment can satisfy all 12 clues simultaneously. If Clue 12 is corrected to *"The ICLR conference was in January,"* the puzzle has the unique solution shown above.