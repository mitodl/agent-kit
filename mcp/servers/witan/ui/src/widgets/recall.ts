import type { RecallResult } from "../types.js";
import { recallResults } from "../views/memory.js";
import { mountWidget } from "./host.js";

mountWidget<RecallResult>("recall", ({ value, args, route, now }) =>
	// `q` only feeds the empty-result message ("Nothing matches …"), so it is
	// the query the model sent rather than a search this widget runs.
	recallResults(value, { ...route, q: String(args.query ?? "") }, now),
);
