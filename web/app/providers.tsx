"use client";

import { HRThemeProvider, ToastProvider } from "@hackerrank/hrds-components";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";

export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 5_000 } },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <HRThemeProvider initialTheme="dark" initialVersion={3}>
        <ToastProvider>{children}</ToastProvider>
      </HRThemeProvider>
    </QueryClientProvider>
  );
}
