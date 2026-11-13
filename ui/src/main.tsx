import '@fontsource-variable/geist'
import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './index.css'

const rootElement = document.getElementById('root')
if (rootElement === null) throw new Error('Missing application root element')

createRoot(rootElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
