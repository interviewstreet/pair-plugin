"use client";

import { useEffect, useState } from "react";

// Panels need a real direction at render time, so the breakpoint has to be state
// rather than CSS. Starts false so the server and first client render agree.
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false);

  useEffect(() => {
    const mql = window.matchMedia(query);
    const sync = () => setMatches(mql.matches);
    sync();
    mql.addEventListener("change", sync);
    return () => mql.removeEventListener("change", sync);
  }, [query]);

  return matches;
}

export const useIsNarrow = () => useMediaQuery("(max-width: 900px)");
