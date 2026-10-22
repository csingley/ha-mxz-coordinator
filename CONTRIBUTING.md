# Contributing to MXZ Coordinator

Thanks for helping. Bug reports, fixes, and ideas are all welcome. Please read this first, especially before you change how MXZ is controlled.

## What MXZ is

MXZ works like a Tesla's cabin climate in Auto. You set one temperature per room, and MXZ decides whether to heat or cool. You never pick a mode.

Home thermostats usually ask for a heat setpoint and a cool setpoint. That confuses most people, and MXZ exists to get rid of it. Simplicity is the product.

## Design principles

- **One target per room.** Each room has a single temperature. MXZ won't add a dual setpoint, a heat/cool range, or a second handle on the thermostat, even as an option.
- **No mode selection.** MXZ picks heat or cool for the whole system. Users don't.
- **The defaults just work.** Someone who only ever sets a temperature should get a comfortable room. Tuning settings like drift belong in the advanced options, and a new one must be optional and keep today's behavior when left blank.
- **Honest state.** What Home Assistant, Apple Home, and Google Home show should match what the heads are really doing.
- **Safe by default.** New behavior that could affect comfort or safety starts off. It also has to survive a Home Assistant restart and respect a fan speed the user picked by hand.

## Before you open a pull request

- **Fixes, reliability, and safety:** go ahead and open a PR.
- **Anything that changes how people control MXZ** (the thermostat tile, setpoints, modes, or new controls): please open an issue first so we can talk it through. A PR that changes the controls without an agreed issue will probably be closed.
- **Tests:** keep the full test suite passing, and add tests for new behavior. Please don't weaken an existing test.
- **Hardware:** say in the PR whether you tested on real hardware.

Contributors are credited in the README. Thanks for helping keep MXZ simple.
