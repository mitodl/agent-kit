import type { ProjectStatus } from "../types.js";
import { statusRollup } from "../views/projects.js";
import { mountWidget } from "./host.js";

mountWidget<ProjectStatus | null>(
	"workflow_project_status",
	({ value, route, now }) => statusRollup(value, route, now),
);
