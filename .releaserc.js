const branch = process.env.GITHUB_REF_NAME || '';

const plugins = [
  ['@semantic-release/commit-analyzer', {
    preset: 'conventionalcommits',
    releaseRules: [
      { type: 'docs', scope: 'README', release: 'patch' },
      { type: 'refactor', release: 'patch' },
      { type: 'style', release: 'patch' },
    ],
  }],

  ['semantic-release-jira-notes', {
    jiraHost: 'ironsail.atlassian.net',
    ticketPrefixes: ['GO'],
    preset: 'conventionalcommits',
    presetConfig: {
      types: [
        { type: 'feat', section: 'Features' },
        { type: 'fix', section: 'Bug Fixes' },
        { type: 'chore', hidden: true },
        { type: 'docs', section: 'Documentation' },
        { type: 'style', hidden: true },
        { type: 'refactor', section: 'Code Refactoring' },
        { type: 'perf', section: 'Performance Improvements' },
        { type: 'test', section: 'Tests' },
      ],
    },
  }],
];

if (branch === 'main') {
  plugins.push(['@semantic-release/changelog', {
    changelogFile: 'CHANGELOG.md',
  }]);
}

// Two things happen in prepare. The first stamps the version into the product
// metadata and the published installer. The second folds `changelog.d/` into
// `docs/release-notes.md` -- the page mkdocs publishes, grouped by audience --
// and deletes the fragments it consumed, so both halves land in the release
// commit. Only on main: staging is built without running semantic-release, and
// a release assembles a version exactly once.
const prepareCmd =
  branch === 'main'
    ? 'scripts/update-helm-values.sh ${nextRelease.version} ${branch.name} && ' +
      'python3 scripts/changelog_fragments.py assemble --version ${nextRelease.version}'
    : 'scripts/update-helm-values.sh ${nextRelease.version} ${branch.name}';

plugins.push(
  // Synchronize release metadata. Deployment image tags are promoted by the
  // workflow only after both release images have passed their blocking scan.
  ['@semantic-release/exec', { prepareCmd }],

  ['@semantic-release/git', {
    assets: [
      // `CHANGELOG.md` is the developer record; `docs/release-notes.md` is
      // what a reader gets. `changelog.d` is listed so the DELETIONS of the
      // fragments prepare consumed are staged with the release commit --
      // otherwise the next release assembles them a second time.
      ...(branch === 'main' ? ['CHANGELOG.md', 'docs/release-notes.md', 'changelog.d'] : []),
      'package.json',
      'package-lock.json',
      'pyproject.toml',
      'uv.lock',
      'robothor/__init__.py',
      'app/package.json',
      'helm/genus-os/Chart.yaml',
      // Stamped by prepareCmd above: the published installer must ship the
      // version it was released with.
      'scripts/install.sh',
    ],
    message: 'chore(release): ${nextRelease.version} [skip ci]',
  }],

  '@semantic-release/github',
);

module.exports = {
  branches: [
    { name: 'main', channel: 'release' },
    // staging is deployed via the build-and-push job in deploy-release.yml
    // without running semantic-release: image is tagged sha-<short> + :staging
    // and helm/genus-os/values-staging.yaml is bumped directly. No prerelease
    // tags, no GH releases, no CHANGELOG churn on staging.
  ],
  plugins,
};
