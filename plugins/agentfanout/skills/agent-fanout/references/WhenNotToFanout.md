# When not to fan out

Do the work directly when it is a simple answer, one small edit, tightly
sequential, faster than coordination, or unsafe to delegate. Keep secrets,
private data, credentials, git mutation, destructive actions, and irreversible
external effects in the parent.

Do not create workers merely to announce that no fanout is needed. Do not keep
a provider choice sticky across unrelated tasks. Do not add a validator to
routine low-risk work. Do not claim savings without measured comparable runs.
