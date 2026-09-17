#!/usr/bin/env node

import fs from 'node:fs/promises';
import path from 'node:path';

const CONFIG_FILE = path.join(process.cwd(), '.hue-config.json');
const EXPORT_FILE = path.join(process.cwd(), 'hue-export.json');

function printHelp() {
  console.log(`
Hue Philips helper

Usage:
  node index.js discover
  node index.js token --bridge-ip 192.168.1.10
  node index.js export --bridge-ip 192.168.1.10
  node index.js help

Notes:
  - Press the link button on the Hue Bridge before creating the token.
  - The exported JSON is saved as hue-export.json in this folder.
`);
}

function parseArgs(argv) {
  const flags = {};
  const command = argv[0] || 'help';

  for (let i = 1; i < argv.length; i += 1) {
    const current = argv[i];

    if (current === '--bridge-ip') {
      flags.bridgeIp = argv[i + 1];
      i += 1;
    } else if (current === '--devicetype') {
      flags.devicetype = argv[i + 1];
      i += 1;
    } else if (current === '--help' || current === '-h') {
      flags.help = true;
    }
  }

  return { command, flags };
}

async function loadConfig() {
  try {
    const raw = await fs.readFile(CONFIG_FILE, 'utf8');
    return JSON.parse(raw);
  } catch {
    return {};
  }
}

async function saveConfig(config) {
  await fs.writeFile(CONFIG_FILE, JSON.stringify(config, null, 2) + '\n', 'utf8');
}

async function discoverBridges() {
  const response = await fetch('https://discovery.meethue.com/', {
    headers: { Accept: 'application/json' }
  });

  if (!response.ok) {
    throw new Error(`Could not discover Hue bridge: ${response.status} ${response.statusText}`);
  }

  const bridges = await response.json();
  if (!Array.isArray(bridges) || bridges.length === 0) {
    throw new Error('No Hue bridge found on the local network.');
  }

  return bridges;
}

async function resolveBridgeIp(explicitIp) {
  if (explicitIp) return explicitIp;

  const config = await loadConfig();
  if (config.bridgeIp) return config.bridgeIp;

  const bridges = await discoverBridges();
  const bridge = bridges[0];
  const ip = bridge?.internalipaddress || bridge?.ipaddress || bridge?.address;

  if (!ip) {
    throw new Error('No bridge IP could be resolved.');
  }

  return ip;
}

async function setBridgeIp(ip) {
  const config = await loadConfig();
  config.bridgeIp = ip;
  await saveConfig(config);
}

async function setUsername(username) {
  const config = await loadConfig();
  config.username = username;
  await saveConfig(config);
}

async function getUsername() {
  const config = await loadConfig();
  if (config.username) return config.username;
  throw new Error('No username saved yet. Run: node index.js token --bridge-ip 192.168.1.10');
}

async function createToken(ip, devicetype = 'hue-philips#local') {
  const response = await fetch(`http://${ip}/api`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ devicetype })
  });

  if (!response.ok) {
    throw new Error(`Failed to create token: ${response.status} ${response.statusText}`);
  }

  const body = await response.json();
  const success = Array.isArray(body) ? body[0]?.success : body?.success;

  if (!success?.username) {
    const error = Array.isArray(body) ? body[0]?.error : body?.error;
    if (error?.type === 101) {
      throw new Error('Press the link button on the Hue Bridge to authorize the app.');
    }
    throw new Error(`No valid username returned: ${JSON.stringify(body)}`);
  }

  const username = success.username;
  await setBridgeIp(ip);
  await setUsername(username);

  console.log('Token generated successfully.');
  console.log(`Bridge IP: ${ip}`);
  console.log(`Username: ${username}`);
  return username;
}

async function requestHue(ip, pathName, username, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set('hue-application-key', username);
  if (options.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  const response = await fetch(`http://${ip}${pathName}`, { ...options, headers });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Hue API error for ${pathName}: ${response.status} ${response.statusText} ${text}`);
  }

  return response;
}

async function exportBridgeJson(ip, username, outputFile = EXPORT_FILE) {
  let payload;
  let exportData;

  try {
    const response = await requestHue(ip, '/clip/v2/resource', username);
    payload = await response.json();
    exportData = Array.isArray(payload)
      ? payload
      : Array.isArray(payload?.data)
        ? payload.data
        : Array.isArray(payload?.resources)
          ? payload.resources
          : [];
  } catch (error) {
    if (!error.message.includes('404 Not Found')) throw error;

    const response = await requestHue(ip, `/api/${username}`, username);
    exportData = await response.json();
  }

  await fs.writeFile(outputFile, JSON.stringify(exportData, null, 2) + '\n', 'utf8');
  console.log(`JSON exported to ${outputFile}`);
  console.log(`Resources saved: ${Array.isArray(exportData) ? exportData.length : 'full v1 configuration'}`);
  return exportData;
}

async function main() {
  const { command, flags } = parseArgs(process.argv.slice(2));

  if (command === 'help' || command === '--help' || flags.help) {
    printHelp();
    return;
  }

  try {
    if (command === 'discover') {
      const bridges = await discoverBridges();
      console.log(JSON.stringify(bridges, null, 2));
      return;
    }

    if (command === 'token') {
      const bridgeIp = await resolveBridgeIp(flags.bridgeIp);
      const username = await createToken(bridgeIp, flags.devicetype || 'hue-philips#local');
      console.log(`Token saved. Username: ${username}`);
      return;
    }

    if (command === 'export') {
      const bridgeIp = await resolveBridgeIp(flags.bridgeIp);
      const username = await getUsername();
      await exportBridgeJson(bridgeIp, username);
      return;
    }

    printHelp();
  } catch (error) {
    console.error('Error:', error.message || error);
    process.exitCode = 1;
  }
}

main();
