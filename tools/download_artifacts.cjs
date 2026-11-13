// Downloads artifacts of this workflow run by name, as zip files <dir>/<name>.zip, for
// actions/github-script (staged builds, see tools/staged_build.py):
//
//   await require('./tools/download_artifacts.cjs')({ names, dir, core })
//
// download-artifact's `pattern:` filters the run's artifact list, which ends at 1,000
// artifacts; looking each one up by name has no such limit. These are the artifact
// service's calls that @actions/artifact makes for a download by name (ListArtifacts with a
// name filter, then GetSignedArtifactURL), with the runtime token only JavaScript actions
// get. Returns the names that weren't found.
const fs = require('fs')
const path = require('path')
const { Readable } = require('stream')
const { pipeline } = require('stream/promises')

const SERVICE = 'github.actions.results.api.v1.ArtifactService'
const PARALLEL = 8
const ATTEMPTS = 5
const RETRY_STATUS = [429, 500, 502, 503, 504]

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

// the run's and this job's ids in the artifact service, from the token's Actions.Results scope
function backendIds(token) {
  const claims = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString())
  for (const scope of (claims.scp || '').split(' ')) {
    const [name, run, job] = scope.split(':')
    if (name === 'Actions.Results' && run && job) {
      return { workflow_run_backend_id: run, workflow_job_run_backend_id: job }
    }
  }
  throw new Error('ACTIONS_RUNTIME_TOKEN has no Actions.Results scope')
}

// request(): a fetch() response, read by use(response); network errors and the
// service's transient statuses are retried, waiting longer each time
async function retrying(what, request, use, core) {
  for (let attempt = 1; ; attempt++) {
    let error
    let wait = 5000 * 2 ** (attempt - 1)
    let response
    try {
      response = await request()
    } catch (e) {
      error = e.cause ? `${e.message} (${e.cause.message || e.cause})` : e.message
    }
    if (response) {
      if (response.ok) {
        try {
          return await use(response)
        } catch (e) {
          error = e.message
        }
      } else {
        error = `${response.status} ${await response.text()}`
        if (!RETRY_STATUS.includes(response.status)) throw new Error(`${what}: ${error}`)
        wait = Number(response.headers.get('retry-after')) * 1000 || wait
      }
    }
    if (attempt === ATTEMPTS) throw new Error(`${what}: ${error} (after ${ATTEMPTS} attempts)`)
    core.info(`${what}: ${error}; retrying in ${wait / 1000} s`)
    await sleep(wait)
  }
}

module.exports = async function download({ names, dir, core }) {
  const token = process.env.ACTIONS_RUNTIME_TOKEN
  const results = process.env.ACTIONS_RESULTS_URL
  if (!token || !results) throw new Error('ACTIONS_RUNTIME_TOKEN and ACTIONS_RESULTS_URL are only set in JavaScript actions')
  const ids = backendIds(token)
  const origin = new URL(results).origin

  const call = (method, body) => retrying(method, () => fetch(`${origin}/twirp/${SERVICE}/${method}`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }), (response) => response.json(), core)

  fs.mkdirSync(dir, { recursive: true })
  const missing = []
  const todo = [...new Set(names)]
  const worker = async () => {
    for (let name; (name = todo.shift()) !== undefined;) {
      const { artifacts = [] } = await call('ListArtifacts', { ...ids, name_filter: name })
      if (!artifacts.length) {
        missing.push(name)
        continue
      }
      // the newest, as @actions/artifact takes (re-runs can upload a name again)
      const artifact = artifacts.sort((a, b) => Number(b.database_id) - Number(a.database_id))[0]
      const { signed_url: url } = await call('GetSignedArtifactURL', {
        workflow_run_backend_id: artifact.workflow_run_backend_id,
        workflow_job_run_backend_id: artifact.workflow_job_run_backend_id,
        name: artifact.name,
      })
      await retrying(`download ${name}`, () => fetch(url), (response) =>
        pipeline(Readable.fromWeb(response.body), fs.createWriteStream(path.join(dir, `${name}.zip`))), core)
      core.info(`${name}: ${artifact.size} bytes`)
    }
  }
  await Promise.all(Array.from({ length: PARALLEL }, worker))
  if (missing.length) core.info(`Not found: ${missing.sort().join(' ')}`)
  return missing
}
