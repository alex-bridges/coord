--------------------------- MODULE CredentialStore ---------------------------
EXTENDS Naturals

\* One item is imported twice under the same Keychain/service/account. Another
\* item already exists at any other address. Availability can change between
\* calls and is stable within one call; the fallback default is available.
\* Payloads represent byte strings. ACLs, UI, concurrent callers and process
\* termination are outside this model.
CONSTANT Mode
ASSUME Mode \in {"Normal", "SkipStatus", "IgnoreAccount"}

Keychains == {"private", "default"}
Services == {"serviceA", "serviceB"}
Accounts == {"accountA", "accountB"}
Slots == Keychains \X Services \X Accounts
Payloads == {"first", "second"}
Empty == "empty"
NeighborValue == "neighbor"

VARIABLES target, payload, available, call, phase, store, before, readback
vars == <<target, payload, available, call, phase, store, before, readback>>
input == <<target, payload, available, call, before>>

Availability(slot) == IF slot[1] = "default" THEN {TRUE} ELSE BOOLEAN
Terminal == {"Created", "Replaced", "Failed"}
Committed == {"ReadCreation", "ReadReplacement", "Created", "Replaced"}

Init ==
    /\ target \in Slots
    /\ payload \in Payloads
    /\ available \in Availability(target)
    /\ \E neighbor \in Slots \ {target}:
          store = [slot \in Slots |-> IF slot = neighbor THEN NeighborValue ELSE Empty]
    /\ before = store
    /\ call = 1
    /\ phase = "Open"
    /\ readback = Empty

\* SecKeychainOpen can yield a reference even when the file is unusable.
OpenReference ==
    /\ phase = "Open"
    /\ phase' = "CheckStatus"
    /\ UNCHANGED <<input, store, readback>>

CheckStatus ==
    /\ phase = "CheckStatus"
    /\ phase' = IF available \/ Mode = "SkipStatus" THEN "Create" ELSE "Failed"
    /\ UNCHANGED <<input, store, readback>>

\* SecKeychainItemCreateFromContent may redirect an unusable reference.
Create ==
    /\ phase = "Create"
    /\ LET destination == IF available THEN target ELSE <<"default", target[2], target[3]>>
       IN IF store[destination] = Empty
          THEN /\ store' = [store EXCEPT ![destination] = payload]
               /\ phase' = "ReadCreation"
          ELSE /\ UNCHANGED store
               /\ phase' = "Duplicate"
    /\ UNCHANGED <<input, readback>>

ReplaceDuplicate ==
    /\ phase = "Duplicate"
    /\ LET matches == {slot \in Slots :
              /\ slot[1] = target[1] /\ slot[2] = target[2]
              /\ (slot[3] = target[3] \/ Mode = "IgnoreAccount")
              /\ store[slot] # Empty}
       IN IF available /\ matches # {}
          THEN /\ \E item \in matches: store' = [store EXCEPT ![item] = payload]
               /\ phase' = "ReadReplacement"
          ELSE /\ UNCHANGED store
               /\ phase' = "Failed"
    /\ UNCHANGED <<input, readback>>

\* An ACL/setup/lookup or native write error before committing any bytes.
FailBeforeWrite ==
    /\ phase \in {"Create", "Duplicate"}
    /\ phase' = "Failed"
    /\ UNCHANGED <<input, store, readback>>

\* Independent reader uses the caller's original three-part address.
ReadItem ==
    /\ phase \in {"ReadCreation", "ReadReplacement"}
    /\ readback' = store[target]
    /\ phase' = IF phase = "ReadCreation" THEN "Created" ELSE "Replaced"
    /\ UNCHANGED <<input, store>>

NextImport ==
    /\ phase \in Terminal /\ call = 1
    /\ call' = 2
    /\ payload' \in Payloads
    /\ available' \in Availability(target)
    /\ before' = store
    /\ readback' = Empty
    /\ phase' = "Open"
    /\ UNCHANGED <<target, store>>

Next == OpenReference \/ CheckStatus \/ Create \/ ReplaceDuplicate \/ FailBeforeWrite
        \/ ReadItem \/ NextImport \/ (phase \in Terminal /\ call = 2 /\ UNCHANGED vars)
Spec == Init /\ [][Next]_vars

\* These expectations depend only on the original request and the store at
\* call entry. They do not use the native destination or duplicate lookup.
UnavailableStoreIsRejected ==
    ~available => (store = before /\ phase \notin Committed)

StoreMatchesRequest ==
    IF phase \in Committed
    THEN store = [before EXCEPT ![target] = payload]
    ELSE store = before

ReadbackMatches == phase \in {"Created", "Replaced"} => readback = payload

\* Deliberately false reachability assertion for a changed payload after the
\* first import created this item. The initially populated neighbor survives.
NeverReplacedWithNeighbor ==
    ~(phase = "Replaced" /\ call = 2 /\ before[target] \in Payloads
      /\ before[target] # payload /\ readback = payload
      /\ \E neighbor \in Slots \ {target}: before[neighbor] = NeighborValue
                                          /\ store[neighbor] = NeighborValue)
=============================================================================
