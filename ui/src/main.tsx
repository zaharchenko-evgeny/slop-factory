import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./index.css";
import { registerBuiltinViews } from "./views/builtin";

registerBuiltinViews();
createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
