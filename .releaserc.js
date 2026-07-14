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

plugins.push(
  // Synchronize release metadata. Deployment image tags are promoted by the
  // workflow only after both release images have passed their blocking scan.
  ['@semantic-release/exec', {
    prepareCmd: 'scripts/update-helm-values.sh ${nextRelease.version} ${branch.name}',
  }],

  ['@semantic-release/git', {
    assets: [
      ...(branch === 'main' ? ['CHANGELOG.md'] : []),
      'package.json',
      'package-lock.json',
      'pyproject.toml',
      'uv.lock',
      'robothor/__init__.py',
      'app/package.json',
      'helm/genus-os/Chart.yaml',
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
