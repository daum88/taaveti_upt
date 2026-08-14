export const startDelegatedActions = ({ clickActions, changeActions = {} }) => {
  const onClick = (event) => {
    const target = event.target.closest('[data-action]');
    const action = target && clickActions[target.dataset.action];
    if (action) action(target.dataset.arg);
  };

  const onChange = (event) => {
    const target = event.target.closest('[data-change-action]');
    const action = target && changeActions[target.dataset.changeAction];
    if (action) action(target);
  };

  document.addEventListener('click', onClick);
  document.addEventListener('change', onChange);

  return () => {
    document.removeEventListener('click', onClick);
    document.removeEventListener('change', onChange);
  };
};
