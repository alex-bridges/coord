---------------------------- MODULE StarterGuard ----------------------------
EXTENDS Naturals, Sequences

CONSTANT Mode
ASSUME Mode \in {"Normal", "AlwaysAllow", "AlwaysDeny"}

\* A single hook invocation has no memory of previous decisions. Inputs are
\* already parsed tool families, recognized Bash actions and resolved path
\* components. The shell parser, filesystem and OS sandbox are outside this model.
Call(tool, arg) == [tool |-> tool, arg |-> arg]

Examples ==
    [ BadEnvelope      |-> Call("Malformed", ""),
      MissingCommand   |-> Call("Bash", ""),
      MissingPath      |-> Call("Edit", <<>>),
      ViewPR           |-> Call("Bash", "viewPR"),
      MergePR          |-> Call("Bash", "mergePR"),
      PushFeature      |-> Call("Bash", "pushFeature"),
      AccessTokenCache |-> Call("Bash", "accessTokenCache"),
      CatInstructions  |-> Call("Bash", "readInstructions"),
      TeeInstructions  |-> Call("Bash", "writeInstructions"),
      EditInstructions |-> Call("Edit", <<"project", "CLAUDE.md">>),
      EditHook         |-> Call("Edit", <<"project", ".claude", "hooks", "check.py">>),
      EditHomeSettings |-> Call("Edit", <<"home", ".claude", "settings.json">>),
      EditSibling      |-> Call("Edit", <<"project", "CLAUDE.md.example">>),
      EditSource       |-> Call("Edit", <<"project", "src", "module.py">>),
      EditReview       |-> Call("Edit", <<"state", "reviews", "pr-7", "notes">>),
      EditScratch      |-> Call("Edit", <<"state", "reviews", "pr-7-scratch", "notes">>),
      EditReviewRoot   |-> Call("Edit", <<"state", "reviews">>),
      EditReviewSibling |-> Call("Edit", <<"state", "reviews-backup", "notes">>),
      ReadInstructions |-> Call("Read", <<"project", "CLAUDE.md">>),
      UnknownTool      |-> Call("Unknown", "") ]

\* Model the path comparisons on components: exact files, directory contents
\* and strict descendants of the review directory have different boundaries.
Within(path, dir) == Len(path) >= Len(dir) /\ SubSeq(path, 1, Len(dir)) = dir

Protected(path) ==
    \/ path \in {<<"project", "CLAUDE.md">>, <<"home", ".claude", "settings.json">>}
    \/ Within(path, <<"project", ".claude", "hooks">>)

ReviewPath(path) == Within(path, <<"state", "reviews">>) /\ Len(path) > 2

Guard(agent, wake, call) ==
    IF agent = "Other" /\ ~wake THEN "Allow"
    ELSE CASE call.tool = "Malformed" -> "Deny"
           [] call.tool = "Bash" ->
                IF call.arg \in {"", "mergePR", "accessTokenCache"}
                    \/ (agent = "Reviewer" /\ call.arg = "pushFeature")
                    \/ (agent # "Reviewer" /\ call.arg = "writeInstructions")
                THEN "Deny" ELSE "Allow"
           [] call.tool = "Edit" ->
                IF call.arg = <<>> \/ (IF agent = "Reviewer"
                                         THEN ~ReviewPath(call.arg)
                                         ELSE Protected(call.arg))
                THEN "Deny" ELSE "Allow"
           [] OTHER -> "Allow"

VARIABLES agent, wake, example, result
vars == <<agent, wake, example, result>>
Agents == {"Other", "Conductor", "Reviewer"}

Init ==
    /\ agent \in Agents
    /\ wake \in BOOLEAN
    /\ example \in DOMAIN Examples
    /\ result = "Pending"

Next ==
    /\ UNCHANGED <<agent, wake, example>>
    /\ result' = CASE Mode = "AlwaysAllow" -> "Allow"
                   [] Mode = "AlwaysDeny" -> "Deny"
                   [] OTHER -> Guard(agent, wake, Examples[example])

Spec == Init /\ [][Next]_vars

\* Independent expected outcomes, indexed by example. Entries name ALL the
\* receiver/wake pairs that should allow it. No guard decision or path logic
\* is used to construct this table.
AllContexts == Agents \X BOOLEAN
Inactive == {<<"Other", FALSE>>}
ConductorOrInactive == Inactive \cup {<<"Other", TRUE>>, <<"Conductor", FALSE>>, <<"Conductor", TRUE>>}
ReviewerOrInactive == Inactive \cup {<<"Reviewer", FALSE>>, <<"Reviewer", TRUE>>}

ExpectedAllows ==
    [ BadEnvelope      |-> Inactive,
      MissingCommand   |-> Inactive,
      MissingPath      |-> Inactive,
      ViewPR           |-> AllContexts,
      MergePR          |-> Inactive,
      PushFeature      |-> ConductorOrInactive,
      AccessTokenCache |-> Inactive,
      CatInstructions  |-> AllContexts,
      \* Reviewer Bash filesystem confinement is enforced by its OS sandbox.
      TeeInstructions  |-> ReviewerOrInactive,
      EditInstructions |-> Inactive,
      EditHook         |-> Inactive,
      EditHomeSettings |-> Inactive,
      EditSibling      |-> ConductorOrInactive,
      EditSource       |-> ConductorOrInactive,
      EditReview       |-> AllContexts,
      EditScratch      |-> AllContexts,
      EditReviewRoot   |-> ConductorOrInactive,
      EditReviewSibling |-> ConductorOrInactive,
      ReadInstructions |-> AllContexts,
      UnknownTool      |-> AllContexts ]

TypeOK ==
    /\ agent \in Agents /\ wake \in BOOLEAN /\ example \in DOMAIN Examples
    /\ result \in {"Pending", "Allow", "Deny"}
    /\ DOMAIN ExpectedAllows = DOMAIN Examples

RequiredDenials == result = "Allow" => <<agent, wake>> \in ExpectedAllows[example]
RequiredAllows  == result = "Deny"  => <<agent, wake>> \notin ExpectedAllows[example]

\* Optional counterexample probe: wake alone activates conductor restrictions.
NeverWakeDeniesMerge == ~(agent = "Other" /\ wake /\ example = "MergePR" /\ result = "Deny")
=============================================================================
