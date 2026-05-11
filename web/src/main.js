import App from './App.svelte'
import { mount } from 'svelte'
import './app.css'
import { installJsonPreviewAutoFit } from './lib/jsonPreviewAutoFit.js'

installJsonPreviewAutoFit()

const app = mount(App, { target: document.getElementById('app') })

export default app
