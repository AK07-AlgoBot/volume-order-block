import { useEffect, useMemo, useRef, useState } from "react";
import { ClosedTradesTable } from "./components/ClosedTradesTable";
import { LiveTradesTable } from "./components/LiveTradesTable";
import { WeeklyPnlChart } from "./components/WeeklyPnlChart";

const API_BASE = "http://localhost:8000";

function getLocalDateIso() {
  const now = new Date();
  const offsetMs = now.getTimezoneOffset() * 60000;
  return new Date(now.getTime() - offsetMs).toISOString().slice(0, 10);
}

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
  const [todayDateText] = useState(getLocalDateIso);
  const [liveTrades, setLiveTrades] = useState([]);
  const [closedTrades, setClosedTrades] = useState([]);
  const [closedTradeDates, setClosedTradeDates] = useState([]);
  const [selectedClosedDate, setSelectedClosedDate] = useState(todayDateText);
  const [weeklyPnl, setWeeklyPnl] = useState([]);
  const [connected, setConnected] = useState(false);
  const selectedClosedDateRef = useRef(selectedClosedDate);
  const appReadySentRef = useRef(false);

  const markAppReady = () => {
    if (appReadySentRef.current) {
      return;
    }
    appReadySentRef.current = true;
    window.dispatchEvent(new Event("ak07-app-ready"));
  };

  useEffect(() => {
    selectedClosedDateRef.current = selectedClosedDate;
  }, [selectedClosedDate]);

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
        setClosedTradeDates(payload.closed_trade_dates || []);
        setSelectedClosedDate(payload.closed_trade_selected_date || todayDateText);
        setWeeklyPnl(payload.weekly_pnl || []);
        markAppReady();
      })
      .catch((error) => {
        console.error("Failed initial load", error);
        // Even on failure, remove boot splash so user sees fallback UI instead of blocked overlay.
        markAppReady();
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
          const tradeDate = String(message.trade?.closed_at || "").slice(0, 10);
          if (tradeDate) {
            setClosedTradeDates((prev) =>
              prev.includes(tradeDate) ? prev : [tradeDate, ...prev].sort((a, b) => b.localeCompare(a))
            );
          }
          if (!tradeDate || tradeDate === selectedClosedDateRef.current) {
            setClosedTrades((prev) => upsertTrade(prev, message.trade));
          }
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
  }, [todayDateText]);

  useEffect(() => {
    let active = true;
    fetch(`${API_BASE}/api/dashboard/closed-trades?date=${encodeURIComponent(selectedClosedDate)}`)
      .then((response) => response.json())
      .then((payload) => {
        if (!active) {
          return;
        }
        setClosedTrades(payload.closed_trades || []);
        setClosedTradeDates(payload.closed_trade_dates || []);
      })
      .catch((error) => {
        console.error("Failed closed trades load", error);
      });
    return () => {
      active = false;
    };
  }, [selectedClosedDate]);

  const todayRealized = useMemo(
    () => Number(weeklyPnl.find((point) => point.date === todayDateText)?.pnl || 0),
    [weeklyPnl, todayDateText]
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
          <ClosedTradesTable
            trades={closedTrades}
            availableDates={closedTradeDates}
            selectedDate={selectedClosedDate}
            onDateChange={setSelectedClosedDate}
          />
        </div>

        <div className="subtle" style={{ marginTop: "0.75rem", marginBottom: "1.5rem" }}>
          Today realized P&L: {todayRealized.toFixed(2)}
        </div>
      </main>
    </>
  );
}
