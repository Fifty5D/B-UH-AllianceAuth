import {registerThemeChecks} from "./theme-checks";

// The ordinary browser lane already runs these checks through
// console-theme.spec.ts. Register the screenshot-producing variant only when
// the isolated Preview UI job supplies its bounded output directory.
if (process.env.BUH_PREVIEW_OUTPUT_DIR) {
  registerThemeChecks(true);
}
