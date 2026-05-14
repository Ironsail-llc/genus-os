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

// Helm values bump is intentionally not wired yet — added in Phase 4 once
// helm/genus-os/values*.yaml exist. When ready, insert before
// @semantic-release/git:
//   ['@semantic-release/exec', {
//     prepareCmd: 'scripts/update-helm-values.sh ${nextRelease.version} ${branch.name}',
//   }],

plugins.push(
  ['@semantic-release/git', {
    assets: [
      ...(branch === 'main' ? ['CHANGELOG.md'] : []),
      'package.json',
      'package-lock.json',
    ],
    message: 'chore(release): ${nextRelease.version} [skip ci]',
  }],

  '@semantic-release/github',
);

module.exports = {
  branches: [
    { name: 'main', channel: 'release' },
    { name: 'staging', channel: 'stg', prerelease: 'stg' },
  ],
  plugins,
};
