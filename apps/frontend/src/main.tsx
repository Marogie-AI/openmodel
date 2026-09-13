import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SessionProvider } from "./auth/session";
import App from "./App";
import ErrorBoundary from "./components/ErrorBoundary";
import "./index.css";

const root = document.getElementById("root");
if (root === null) throw new Error("#root is missing from index.html");

// An ApiError is a verdict from the server, not a blip: retrying a 401 or a 422
// just burns the rate limit.
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

createRoot(root).render(
  <StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <SessionProvider>
          <App />
        </SessionProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
);
