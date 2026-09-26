import { useEffect, useState } from 'react'
import { getConfig } from './api'
import type { ConfigResponse } from './types'
import SettingsModal from './components/SettingsModal'
import BackfillPanel from './components/BackfillPanel'
import LeaderboardTab from './components/LeaderboardTab'
import RecoveryTab from './components/RecoveryTab'
import DailyDetailTab from './components/DailyDetailTab'
import TrendsTab from './components/TrendsTab'
import Callout from './components/Callout'

type TabKey = 'leaderboard' | 'recovery' | 'daily' | 'trends'

const TABS: { key: TabKey; label: string }[] = [
  { key: 'leaderboard', label: '🏆 Leaderboard' },
  { key: 'daily', label: 'Daily Detail' },
  { key: 'trends', label: 'Trends' },
  { key: 'recovery', label: 'Recovery' },
]

export default function App() {
  const [config, setConfig] = useState<ConfigResponse | null>(null)
  const [configError, setConfigError] = useState<string | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [activeTab, setActiveTab] = useState<TabKey>('leaderboard')

  useEffect(() => {
    getConfig()
      .then(setConfig)
      .catch((err) => setConfigError(err instanceof Error ? err.message : 'Failed to load settings.'))
  }, [])

  return (
    <div className="app">
      <header className="app-header">
        <h1>Who's Stressing You Out</h1>
        {config && (
          <button
            className="icon-button settings-toggle"
            title="Settings"
            onClick={() => setSettingsOpen(true)}
          >
            {config.ready ? '⋮' : '⚠️'}
          </button>
        )}
      </header>

      {configError && <Callout variant="error">{configError}</Callout>}

      {config && settingsOpen && (
        <SettingsModal config={config} onClose={() => setSettingsOpen(false)} onSaved={setConfig} />
      )}

      <nav className="tab-nav">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            className={`tab-button ${activeTab === tab.key ? 'tab-button-active' : ''}`}
            onClick={() => setActiveTab(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      <main className="tab-content">
        {activeTab === 'leaderboard' && <LeaderboardTab />}
        {activeTab === 'daily' && <DailyDetailTab />}
        {activeTab === 'trends' && <TrendsTab />}
        {activeTab === 'recovery' && <RecoveryTab />}
      </main>

      <BackfillPanel />
    </div>
  )
}
