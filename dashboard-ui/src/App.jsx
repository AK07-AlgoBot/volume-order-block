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

function normalizeIsoSecond(value) {
  const text = String(value || "").trim().replace(",", ".");
  return text.length >= 19 ? text.slice(0, 19) : text;
}

function liveTradeIdentityKey(trade) {
  return [
    trade?.symbol || "",
    trade?.side || "",
    Number(trade?.quantity || 0).toFixed(4),
    Number(trade?.entry_price || 0).toFixed(4),
    normalizeIsoSecond(trade?.opened_at),
  ].join("|");
}

function dedupeLiveTrades(trades) {
  const latestByKey = new Map();
  for (const trade of trades || []) {
    const key = liveTradeIdentityKey(trade);
    const prev = latestByKey.get(key);
    const rank = `${trade?.opened_at || ""}|${trade?.id || ""}`;
    const prevRank = prev ? `${prev?.opened_at || ""}|${prev?.id || ""}` : "";
    if (!prev || rank >= prevRank) {
      latestByKey.set(key, trade);
    }
  }
  return Array.from(latestByKey.values());
}

export default function App() {
  const [todayDateText] = useState(getLocalDateIso);
  const [liveTrades, setLiveTrades] = useState([]);
  const [closedTrades, setClosedTrades] = useState([]);
  const [closedTradeDates, setClosedTradeDates] = useState([]);
  const [selectedClosedDate, setSelectedClosedDate] = useState(todayDateText);
  const [weeklyPnl, setWeeklyPnl] = useState([]);
  const [weeklyTotal, setWeeklyTotal] = useState(0);
  const [weeklyFilterOptions, setWeeklyFilterOptions] = useState([]);
  const [selectedWeekOffset, setSelectedWeekOffset] = useState(0);
  const [connected, setConnected] = useState(false);
  const selectedClosedDateRef = useRef(selectedClosedDate);
  const selectedWeekOffsetRef = useRef(selectedWeekOffset);
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
    selectedWeekOffsetRef.current = selectedWeekOffset;
  }, [selectedWeekOffset]);

  useEffect(() => {
    let active = true;
    const loadWeeklyPnl = (weekOffset = selectedWeekOffsetRef.current) => {
      fetch(`${API_BASE}/api/dashboard/weekly-pnl?week_offset=${encodeURIComponent(weekOffset)}`)
        .then((response) => response.json())
        .then((payload) => {
          if (!active) {
            return;
          }
          setWeeklyPnl(payload.weekly_pnl || []);
          setWeeklyTotal(Number(payload.weekly_total || 0));
          setWeeklyFilterOptions(payload.weekly_filter_options || []);
          setSelectedWeekOffset(Number(payload.weekly_selected_offset || 0));
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
        setLiveTrades(dedupeLiveTrades(payload.live_trades || []));
        setClosedTrades(payload.closed_trades || []);
        setClosedTradeDates(payload.closed_trade_dates || []);
        setSelectedClosedDate(payload.closed_trade_selected_date || todayDateText);
        setWeeklyPnl(payload.weekly_pnl || []);
        setWeeklyTotal(Number(payload.weekly_total || 0));
        setWeeklyFilterOptions(payload.weekly_filter_options || []);
        setSelectedWeekOffset(Number(payload.weekly_selected_offset || 0));
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
          setLiveTrades((prev) => dedupeLiveTrades(upsertTrade(prev, message.trade)));
          return;
        }

        if (message.type === "trades_updated_batch") {
          setLiveTrades((prev) =>
            dedupeLiveTrades(
              message.trades.reduce((acc, trade) => upsertTrade(acc, trade), prev)
            )
          );
          return;
        }

        if (message.type === "trade_closed") {
          setLiveTrades((prev) =>
            dedupeLiveTrades(removeTrade(prev, message.trade.id)).filter(
              (item) => liveTradeIdentityKey(item) !== liveTradeIdentityKey(message.trade)
            )
          );
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
          if (selectedWeekOffsetRef.current === 0) {
            setWeeklyPnl(message.weekly_pnl || []);
            const total = (message.weekly_pnl || []).reduce(
              (sum, point) => sum + Number(point?.pnl || 0),
              0
            );
            setWeeklyTotal(total);
          }
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
    fetch(
      `${API_BASE}/api/dashboard/weekly-pnl?week_offset=${encodeURIComponent(selectedWeekOffset)}`
    )
      .then((response) => response.json())
      .then((payload) => {
        if (!active) {
          return;
        }
        setWeeklyPnl(payload.weekly_pnl || []);
        setWeeklyTotal(Number(payload.weekly_total || 0));
        setWeeklyFilterOptions(payload.weekly_filter_options || []);
      })
      .catch((error) => {
        console.error("Failed selected weekly pnl load", error);
      });
    return () => {
      active = false;
    };
  }, [selectedWeekOffset]);

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
          <WeeklyPnlChart
            points={weeklyPnl}
            weekTotal={weeklyTotal}
            selectedWeekOffset={selectedWeekOffset}
            weekOptions={weeklyFilterOptions}
            onWeekChange={setSelectedWeekOffset}
          />
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
