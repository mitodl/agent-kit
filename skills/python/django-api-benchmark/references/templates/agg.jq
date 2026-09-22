# TEMPLATE - adapt the qname classifier to the app under test.
#
# Aggregate a trace.py capture into one row per logical query, taking the
# MEDIAN of each query position across the traced repeats. One traced request
# is far too noisy to attribute a gap to a query.
#
# Usage:  jq --arg n "$(jq '.repeats' trace-x.json)" -f agg.jq trace-x.json
#
# The per_req column is a correctness check on the classifier: if a label shows
# a non-integer or unexpectedly high count, two different queries are colliding
# under one name and their medians are being mixed. Order the branches
# most-specific first - a broad pattern placed early will swallow later ones
# (e.g. a query that JOINs table X matching an "X" branch before its own).

def med: sort | .[(length/2|floor)];

def qname:
  gsub("\\s+";" ") as $s |
  if   ($s|test("COUNT\\(\\*\\)")) then "COUNT(*) paginator"
  elif ($s|test("DISTINCT \"<main_table>\"")) then "main page query"
  # Mark the queries the change targets so they are obvious in the output.
  elif ($s|test("_prefetch_related_val_<fk>")) then "** <changed> prefetch"
  elif ($s|test("FROM \"<child_table>\"")) then "** <child> prefetch"
  else ($s[0:45]) end;

($n|tonumber) as $reps |
[ .runs[].db[] | {k: (.sql|qname), sql: .dur_ms, gap: .gap_ms} ]
| group_by(.k)
| map({
    k: .[0].k,
    per_req: ((length / $reps)*10|round/10),
    sql: ([.[].sql] | med),
    gap: ([.[].gap] | med),
    tot: ((([.[].sql] | med) + ([.[].gap] | med))*10|round/10)
  })
| sort_by(-.tot)
