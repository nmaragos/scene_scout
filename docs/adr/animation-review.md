# Time alignment and animation change detection

Publish reviewers need to spot motion changes even when two publishes have identical default poses. The first release targets the current Windows/Storm workstation. It delivers timeline scrubbing, navigation to a change, and authored evidence. Its manual acceptance case is the pump bracket with different translations at time code 48. Playback and curve editing are outside this milestone.

The default aligns elapsed seconds from time code zero using each stage’s time codes per second. Reviewers can choose matching numbered time codes instead. Compare rows use A’s time-code units. The timeline keeps A on the left and B on the right, with the numeric editor in the selected stage’s position. Switching stages preserves the review instant. Both the viewport and the comparison follow the chosen alignment.

For the first milestone, an animation change is a difference in composed typed values at an authored sample time from either publish. The comparison includes subframes and static counterparts. This keeps redundant keys out of the change list. Checking between samples or reviewing changes to key placement itself would require a broader sampling or key-comparison policy.
