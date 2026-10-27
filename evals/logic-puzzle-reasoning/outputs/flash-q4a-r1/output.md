# Logic Puzzle: Five Researchers, Five Conferences, Five Months, Five Subfields

## Step 1 — Extract Direct Facts

| Clue | Direct Assignment |
|---|---|
| C1 | **Ada → NeurIPS**, **Ada → Computer Vision** |
| C11 | **Clara → NLP** |
| C2 + C11 | **Clara → June** (NLP is in June) |
| C8 | **Ben → August** |
| C12 | **ICML → March** |
| C5 | **Robotics → ICLR** |
| C7 | **Theory → ACL** |
| C6 | **Eli → January or October** |

---

## Step 2 — Pin Down Ben's Conference

Ben cannot be at **ACL** or **EMNLP** (C3). NeurIPS is Ada's (C1). So:

> Ben ∈ {ICML, ICLR}

But ICML is in **March** (C12) and Ben's conference is in **August** (C8). A conference has one fixed month, so:

> Ben ≠ ICML → **Ben → ICLR**

---

## Step 3 — The First Contradiction (must flag this)

C5 says the Robotics paper is at ICLR. We just placed Ben at ICLR. Therefore Ben's subfield would be **Robotics**.

But C3 explicitly states: *"his paper was not on Theory **or Robotics**."*

> **⚠ Contradiction detected.** The five clues {C1, C3, C5, C8, C12} are mutually inconsistent. No assignment can satisfy all of them simultaneously.

I will proceed by treating **C12 ("ICML was in March")** as the erroneous clue (the most isolated constraint), keeping the other eleven intact. This yields a solution satisfying 11 of 12 clues.

---

## Step 4 — Assign Ben's Subfield

With C12 set aside, Ben is at ICML (not ICLR). His subfield cannot be Theory or Robotics (C3), cannot be CV (Ada), cannot be NLP (Clara). The only option left:

> **Ben → ICML → August → Reinforcement Learning**

---

## Step 5 — Assign Clara's Conference

Remaining conferences for Clara, Dana, Eli: **ACL, EMNLP, ICLR**.

- Clara = NLP. ACL = Theory (C7) → Clara ≠ ACL.
- ICLR = Robotics (C5) → Clara ≠ ICLR.
- Therefore: **Clara → EMNLP → June → NLP**

Check C9: EMNLP is in June, which is ≠ January. ✓

---

## Step 6 — Assign Dana and Eli's Conferences

Remaining conferences: **ACL, ICLR**. Remaining months: **January, March, October** (for Ada, Dana, Eli, since Ben=Aug, Clara=Jun).

- Eli ∈ {January, October} (C6), so Eli ≠ March.
- ICML = August (Ben), so March must go to **Ada** (the only remaining person who can take March).

> **Ada → NeurIPS → March → Computer Vision**

Remaining months for Dana and Eli: **January, October**.

C4 requires **Clara (June) < Dana**. So Dana must be in **October** (the only remaining month after June).

> **Dana → October**, **Eli → January** ✓ (satisfies C6)

---

## Step 7 — Assign Remaining Subfields

Subfields used: CV (Ada), NLP (Clara), RL (Ben). Remaining: **Theory, Robotics** for Dana and Eli.

- ACL = Theory (C7), ICLR = Robotics (C5).
- Dana ∈ {ACL, ICLR}, Eli ∈ {ACL, ICLR}.

Both assignments are consistent with all remaining clues (C10 only says Dana ≠ CV, which is satisfied either way). I'll take the more constrained reading:

> **Dana → ACL → October → Theory**
> **Eli → ICLR → January → Robotics**

*(The alternative — Dana at ICLR/Robotics, Eli at ACL/Theory — also satisfies every clue. The puzzle does not uniquely distinguish these two.)*

---

## Step 8 — Final Table

| Researcher | Conference | Month | Subfield |
|:---:|:---:|:---:|:---:|
| **Ada** | NeurIPS | March | Computer Vision |
| **Ben** | ICML | August | Reinforcement Learning |
| **Clara** | EMNLP | June | NLP |
| **Dana** | ACL | October | Theory |
| **Eli** | ICLR | January | Robotics |

---

## Step 9 — Verify Every Clue

| # | Clue | Check |
|---|---|---|
| C1 | Ada at NeurIPS, CV | ✅ Ada = NeurIPS, CV |
| C2 | NLP paper in June | ✅ Clara (NLP) = June |
| C3 | Ben ∉ {ACL, EMNLP}; Ben ∉ {Theory, Robotics} | ✅ Ben = ICML, RL |
| C4 | Clara's month < Dana's month | ✅ June < October |
| C5 | Robotics at ICLR | ✅ Eli (Robotics) = ICLR |
| C6 | Eli in January or October | ✅ Eli = January |
| C7 | Theory at ACL | ✅ Dana (Theory) = ACL |
| C8 | Ben in August | ✅ Ben = August |
| C9 | EMNLP not in January | ✅ EMNLP = June |
| C10 | Dana not on CV | ✅ Dana = Theory |
| C11 | Clara on NLP | ✅ |
| C12 | ICML in March | ❌ **ICML = August (Ben)** |

**11 of 12 clues satisfied; C12 violated.**

---

## Step 10 — Verdict on Consistency

> **The puzzle as written contains an unavoidable contradiction.** Clues C1, C3, C5, C8, and C12 cannot all be true at once:
> 1. Ben ∉ {ACL, EMNLP, NeurIPS} → Ben ∈ {ICML, ICLR}
> 2. Ben = August ∧ ICML = March → Ben ≠ ICML → **Ben = ICLR**
> 3. ICLR = Robotics → **Ben = Robotics**
> 4. C3: **Ben ≠ Robotics** ← **contradiction**
>
> A second tension arises from C2+C11 (Clara = June) combined with the forced placement of Dana at ICML = March, which violates C4 (June < March is false).
>
> The table above is the **maximally consistent solution** (11/12 clues), obtained by relaxing C12. It is also **not fully unique**: swapping Dana ↔ (Eli's conference/subfield) yields an equally valid second solution.