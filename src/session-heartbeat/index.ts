import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';
import { Dialog } from '@jupyterlab/apputils';
import { URLExt } from '@jupyterlab/coreutils';
import { ServerConnection } from '@jupyterlab/services';

class Heartbeat {
  start() {
    window.clearTimeout(this._heartbeatTimer);
    this._heartbeatTimer = window.setInterval(async () => {
      await this._beat();
    }, this._interval);
    // Immediately check
    void this._beat();
  }

  async _beat() {
    const response = await ServerConnection.makeRequest(
      Private.getUrl(this._serverSettings),
      { method: 'GET' },
      this._serverSettings
    );

    const json = await response.json();

    if (response.status === 200) {
      console.debug(`Session ok until ${json.expires_at}`);
    } else if (response.status === 401) {
      await this._reauthenticate(json.reauthenticate_link);
    }
  }

  async _reauthenticate(redirectUrl: string) {
    const dialog = new Dialog({
      title: 'Your Chameleon session has timed out.',
      body: 'We will attempt to automatically reconnect you.',
      buttons: [Dialog.okButton({ label: 'Continue' })]
    });

    const result = await dialog.launch();
    dialog.dispose();
    if (result.button.accept) {
      document.location.href = redirectUrl;
    }
  }

  private _heartbeatTimer: number = null;
  private _interval = 60000; // ms
  private _serverSettings = ServerConnection.makeSettings();
}

namespace Private {
  export function getUrl(settings: ServerConnection.ISettings): string {
    const parts = [settings.baseUrl, 'chameleon', 'heartbeat'];
    return URLExt.join.call(URLExt, ...parts);
  }
}

const plugin: JupyterFrontEndPlugin<void> = {
  activate(app: JupyterFrontEnd) {
    Promise.all([app.restored])
      .then(async () => {
        const heartbeat = new Heartbeat();
        heartbeat.start();
      })
      .catch(err => {
        console.error(err);
      });
  },
  id: '@chameleoncloud/jupyterlab-chameleon:sessionHeartbeatPlugin',
  autoStart: true
};

export default plugin;
