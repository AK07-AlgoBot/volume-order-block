import { useEffect, useMemo, useState } from "react";
import { ClosedTradesTable } from "./components/ClosedTradesTable";
import { LiveTradesTable } from "./components/LiveTradesTable";
import { WeeklyPnlChart } from "./components/WeeklyPnlChart";

const API_BASE = "http://localhost:8000";

function upsertTrade(list, trade) {
  const index = list.findIndex((item) => item.id === trade.id);
  if (index === -1) {
    return [trade, ...list];
  }
  const cloned = [...list];
  cloned[index] = { ...cloned[index], ...trade };
  return cloned;
}

function removeTrade(list, tradeId) {
  return list.filter((item) => item.id !== tradeId);
}

export default function App() {
  const [liveTrades, setLiveTrades] = useState([]);
  const [closedTrades, setClosedTrades] = useState([]);
  const [weeklyPnl, setWeeklyPnl] = useState([]);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let active = true;
    const loadWeeklyPnl = () => {
      fetch(`${API_BASE}/api/dashboard/weekly-pnl`)
        .then((response) => response.json())
        .then((payload) => {
          if (!active) {
            return;
          }
          setWeeklyPnl(payload.weekly_pnl || []);
        })
        .catch((error) => {
          console.error("Failed weekly pnl load", error);
        });
    };

    fetch(`${API_BASE}/api/dashboard/initial`)
      .then((response) => response.json())
      .then((payload) => {
        if (!active) {
          return;
        }
        setLiveTrades(payload.live_trades || []);
        setClosedTrades(payload.closed_trades || []);
        setWeeklyPnl(payload.weekly_pnl || []);
      })
      .catch((error) => {
        console.error("Failed initial load", error);
      });

    const socket = new WebSocket("ws://localhost:8000/ws/trades");
    socket.onopen = () => setConnected(true);
    socket.onclose = () => setConnected(false);
    socket.onerror = () => setConnected(false);
    socket.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.type === "trade_opened" || message.type === "trade_updated") {
          setLiveTrades((prev) => upsertTrade(prev, message.trade));
          return;
        }

        if (message.type === "trades_updated_batch") {
          setLiveTrades((prev) =>
            message.trades.reduce((acc, trade) => upsertTrade(acc, trade), prev)
          );
          return;
        }

        if (message.type === "trade_closed") {
          setLiveTrades((prev) => removeTrade(prev, message.trade.id));
          setClosedTrades((prev) => upsertTrade(prev, message.trade));
          loadWeeklyPnl();
          return;
        }

        if (message.type === "pnl_update") {
          setWeeklyPnl(message.weekly_pnl || []);
        }
      } catch (error) {
        console.error("WebSocket parse failed", error);
      }
    };

    const keepAlive = window.setInterval(() => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send("ping");
      }
    }, 15000);
    const weeklyRefresh = window.setInterval(() => {
      loadWeeklyPnl();
    }, 30000);

    return () => {
      active = false;
      window.clearInterval(keepAlive);
      window.clearInterval(weeklyRefresh);
      socket.close();
    };
  }, []);

  const todayRealized = useMemo(
    () =>
      closedTrades
        .filter((trade) => (trade.closed_at || "").startsWith(new Date().toISOString().slice(0, 10)))
        .reduce((sum, trade) => sum + Number(trade.realized_pnl || 0), 0),
    [closedTrades]
  );

  return (
    <>
      <header className="header">
        <div className="container header-inner">
          <div className="logo-wrap">
            <div className="logo-box">AK07</div>
            <div>
              <div className="tag">Trading Dashboard</div>
              <h1 className="title">AK07 Live Monitor</h1>
            </div>
          </div>
          <div>
            <span className="status-chip">
              <span className="dot" />
              {connected ? "Live Connected" : "Disconnected"}
            </span>
          </div>
        </div>
      </header>

      <main className="container">
        <div className="layout">
          <LiveTradesTable trades={liveTrades} />
          <WeeklyPnlChart points={weeklyPnl} />
        </div>

        <div className="closed-section">
          <ClosedTradesTable trades={closedTrades} />
        </div>

        <div className="subtle" style={{ marginTop: "0.75rem", marginBottom: "1.5rem" }}>
          Today realized P&L: {todayRealized.toFixed(2)}
        </div>
      </main>
    </>
  );
}
