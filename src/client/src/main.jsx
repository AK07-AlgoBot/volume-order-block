import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

const removeBootSplash = () => {
  const splash = document.getElementById("boot-splash");
  if (splash) {
    splash.remove();
  }
};

ReactDOM.createRoot(document.getElementById("root")).render(<App />);

// Hide splash when app signals readiness.
window.addEventListener("ak07-app-ready", removeBootSplash, { once: true });
// Fallback: never keep splash forever if API is slow/down.
window.setTimeout(removeBootSplash, 8000);
