import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

import { useReview } from "@/lib/store";

afterEach(() => {
  cleanup();
  useReview.getState().reset();
});
