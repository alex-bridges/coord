------------------------------ MODULE MergeTrain ------------------------------
EXTENDS Naturals, Sequences

\* One live Step.run, from fetching a batch through PATCH /git/refs. IDs denote
\* immutable commits. Both the batch ref and the current trunk may move.
CONSTANT Mode
ASSUME Mode \in {"Normal", "SkipTree", "SkipParents", "SkipCount", "SkipCap",
                 "SkipTrust", "IgnoreReceiptTip", "IgnoreCITip", "SkipGreen",
                 "SkipEligibility", "StaleEligibility", "LiveBranch", "ForceUpdate"}

Base == 0
DivergentTrunk == 90   \* A separate descendant of Base, absent from every batch.
Absent == 99
Tips == {Base, 11, 12, 13, 14, 22, 32, 42}

\* The fixture DAG uses one disjoint file addition per PR, so the canonical
\* conflict-free merge-tree operation is set union. Head 5 and head 2 have
\* identical content and distinct SHAs. 21 omits PR 1; the final merge at 22
\* is correct for its actual parents. 32 and 42 exercise the validator's
\* defensive contract on inconsistent declared parents and counts. The real
\* parser normally derives these inputs from the same immutable git log.
Tree(id) ==
    CASE id \in {Base, 21} -> {}
      [] id \in {1, 11} -> {1}
      [] id \in {2, 5, 22} -> {2}
      [] id \in {3, 4} -> {id}
      [] id \in {12, 32, 42} -> {1, 2}
      [] id = 13 -> {1, 2, 3}
      [] id = 14 -> {1, 2, 3, 4}

Parents(id) ==
    CASE id \in {11, 21} -> <<Base, 1>>
      [] id \in {12, 42} -> <<11, 2>>
      [] id = 13 -> <<12, 3>>
      [] id = 14 -> <<13, 4>>
      [] id = 22 -> <<21, 2>>
      [] id = 32 -> <<11, 5>>

\* First-parent rev-list, in order, and the separately supplied member list.
Chain(tip) ==
    CASE tip = Base -> <<>>
      [] tip = 11 -> <<11>>
      [] tip = 12 -> <<11, 12>>
      [] tip = 13 -> <<11, 12, 13>>
      [] tip = 14 -> <<11, 12, 13, 14>>
      [] tip = 22 -> <<21, 22>>
      [] tip = 32 -> <<11, 32>>
      [] tip = 42 -> <<11, 42>>

Members(tip) ==
    CASE tip = Base -> <<>>
      [] tip = 11 -> <<1>>
      [] tip \in {12, 22, 32} -> <<1, 2>>
      [] tip \in {13, 42} -> <<1, 2, 3>>
      [] tip = 14 -> <<1, 2, 3, 4>>

ValidChain(tip) ==
    LET chain == Chain(tip)
        heads == Members(tip)
    IN  /\ Mode = "SkipCap" \/ Len(heads) \in 1..3
        /\ Mode = "SkipCount" \/ Len(chain) = Len(heads)
        /\ \A i \in (DOMAIN chain \cap DOMAIN heads) :
             LET parent == IF i = 1 THEN Base ELSE chain[i - 1]
             IN  /\ Mode = "SkipParents" \/ Parents(chain[i]) = <<parent, heads[i]>>
                 /\ Mode = "SkipTree" \/ Tree(chain[i]) = Tree(parent) \cup Tree(heads[i])
        /\ (IF Len(chain) = 0 THEN Base ELSE chain[Len(chain)]) = tip

\* The only possible prior trunk tips are Base, an intermediate merge (11),
\* and the divergent descendant. This suffices for the non-force server check.
FastForward(old, target) ==
    old = Base \/ \E i \in DOMAIN Chain(target) : Chain(target)[i] = old

OtherTip(tip) == IF tip = 11 THEN 12 ELSE 11
Receipts(tip) ==
    {[sha |-> tip, trusted |-> TRUE], [sha |-> tip, trusted |-> FALSE],
     [sha |-> OtherTip(tip), trusted |-> TRUE]}
Runs(tip) ==
    {[sha |-> tip, result |-> "green"], [sha |-> tip, result |-> "red"],
     [sha |-> tip, result |-> "foreign"], [sha |-> OtherTip(tip), result |-> "green"]}

\* One PR may have a different head, or lose eligibility (closed, dequeued,
\* no longer reviewed, etc.). PR numbers 1..4 initially have heads 1..4.
AllReady == [pr |-> 0, change |-> "none"]
PRViews == {AllReady} \cup [pr : 1..4, change : {"head", "held"}]
ObservedHead(view, pr) == IF view.pr = pr /\ view.change = "head" THEN 5 ELSE pr
Eligible(view, i) == view.pr # i \/ view.change # "held"

VARIABLES phase, batch, trunk, captured, fetchedTrunk, receipt, ci, prs, fetchedPRs, checked, updateFrom
vars == <<phase, batch, trunk, captured, fetchedTrunk, receipt, ci, prs, fetchedPRs, checked, updateFrom>>

Init ==
    /\ phase = "fetch"
    /\ batch \in Tips
    /\ trunk \in {Base, 11, DivergentTrunk}
    /\ prs \in PRViews
    /\ captured = Absent /\ fetchedTrunk = Absent /\ updateFrom = Absent
    /\ receipt = [sha |-> Absent, trusted |-> FALSE]
    /\ ci = [sha |-> Absent, result |-> "red"]
    /\ fetchedPRs = AllReady /\ checked = AllReady

Fetch ==
    /\ phase = "fetch"
    /\ phase' = "validate"
    /\ captured' = batch /\ fetchedTrunk' = trunk /\ fetchedPRs' = prs
    /\ receipt' \in Receipts(batch) /\ ci' \in Runs(batch)
    /\ UNCHANGED <<batch, trunk, prs, checked, updateFrom>>

CanValidate ==
    /\ Len(Members(captured)) > 0
    /\ Mode = "SkipTrust" \/ receipt.trusted
    /\ Mode = "IgnoreReceiptTip" \/ receipt.sha = captured
    /\ ValidChain(captured)
    /\ Mode = "IgnoreCITip" \/ ci.sha = captured
    /\ Mode = "SkipGreen" \/ ci.result = "green"
    /\ fetchedTrunk = Base
    /\ Mode = "SkipEligibility" \/
         LET view == IF Mode = "StaleEligibility" THEN fetchedPRs ELSE prs
         IN \A i \in DOMAIN Members(captured) :
                Eligible(view, i) /\ ObservedHead(view, i) = Members(captured)[i]

Validate ==
    /\ phase = "validate"
    /\ phase' = IF CanValidate THEN "submit" ELSE "rejected"
    /\ checked' = prs
    /\ UNCHANGED <<batch, trunk, captured, fetchedTrunk, receipt, ci, prs, fetchedPRs, updateFrom>>

\* Ref changes after capture commute with local validation of immutable SHAs.
\* Changes to PR eligibility after its final API read are intentionally
\* possible: GitHub's ref PATCH is not an atomic conditional PR operation.
MoveBatch ==
    /\ phase = "submit" /\ batch = captured
    /\ batch' = OtherTip(captured)
    /\ UNCHANGED <<phase, trunk, captured, fetchedTrunk, receipt, ci, prs, fetchedPRs, checked, updateFrom>>
MoveTrunk ==
    /\ phase = "submit" /\ trunk = Base
    /\ trunk' \in {11, DivergentTrunk}
    /\ UNCHANGED <<phase, batch, captured, fetchedTrunk, receipt, ci, prs, fetchedPRs, checked, updateFrom>>
ChangePR ==
    /\ phase \in {"validate", "submit"} /\ prs = AllReady
    /\ prs' \in PRViews \ {AllReady}
    /\ UNCHANGED <<phase, batch, trunk, captured, fetchedTrunk, receipt, ci, fetchedPRs, checked, updateFrom>>

Submit ==
    LET target == IF Mode = "LiveBranch" THEN batch ELSE captured
        accepted == Mode = "ForceUpdate" \/ FastForward(trunk, target)
    IN  /\ phase = "submit"
        /\ updateFrom' = IF accepted THEN trunk ELSE Absent
        /\ trunk' = IF accepted THEN target ELSE trunk
        /\ phase' = IF accepted THEN "advanced" ELSE "rejected"
        /\ UNCHANGED <<batch, captured, fetchedTrunk, receipt, ci, prs, fetchedPRs, checked>>

Terminal == phase \in {"advanced", "rejected"} /\ UNCHANGED vars
Next == Fetch \/ Validate \/ MoveBatch \/ MoveTrunk \/ ChangePR \/ Submit \/ Terminal

\* Independent acceptance oracle: these three fixture SHAs are precisely the
\* intended canonical 1-, 2-, and 3-member batches. It neither re-runs
\* ValidChain nor uses the transition's FastForward or eligibility predicates.
SafeAdvance ==
    phase = "advanced" =>
        /\ trunk \in {11, 12, 13}
        /\ trunk = captured
        /\ receipt.trusted /\ receipt.sha = trunk
        /\ ci.result = "green" /\ ci.sha = trunk
        /\ fetchedTrunk = Base
        /\ updateFrom \in {Base, 11}
        /\ checked.pr = 0 \/ checked.pr > trunk - 10

\* Extra invariants for reachability probes: a counterexample is a witness.
NeverAdvanceOne == ~(phase = "advanced" /\ trunk = 11)
NeverAdvanceTwo == ~(phase = "advanced" /\ trunk = 12)
NeverAdvanceThree == ~(phase = "advanced" /\ trunk = 13)
NeverAdvanceAfterBranchMove == ~(phase = "advanced" /\ batch # captured)
NeverAdvanceAfterPRChange == ~(phase = "advanced" /\ checked = AllReady /\ prs.pr = 1)
=============================================================================
