import { StrictMode, useEffect } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App";
import { Login, useMe } from "./views/Lan";
import { NotionCallback } from "./views/NotionSource";
import { startTheme } from "./theme/theme";

/** 로컬망 공유 (TP-152): a LAN visitor without a session gets the login screen; 127.0.0.1 goes straight in */
function Gate() {
  const [me, reload] = useMe();
  useEffect(() => void (me && startTheme()), [me]); // /api/themes needs the session on a LAN visit
  if (me === undefined) return null;
  if (!me) return <Login onDone={reload} />;
  return location.pathname === "/notion/callback" ? <NotionCallback /> : <App me={me} />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Gate />
  </StrictMode>,
);
