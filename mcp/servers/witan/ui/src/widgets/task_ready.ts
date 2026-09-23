import type { TaskRow } from "../types.js";
import { readyColumn } from "../views/board.js";
import { mountWidget } from "./host.js";

mountWidget<TaskRow[]>("task_ready", ({ value, route, now }) =>
	readyColumn(value, route, now),
);
