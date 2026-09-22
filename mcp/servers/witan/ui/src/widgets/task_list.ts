import type { TaskRow } from "../types.js";
import { taskGroups } from "../views/projects.js";
import { mountWidget } from "./host.js";

mountWidget<TaskRow[]>("task_list", ({ value, route, now }) =>
	taskGroups(value, route, now),
);
