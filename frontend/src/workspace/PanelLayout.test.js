import {panelSizes} from './PanelLayout';
test('panels first collapse projects, then narrow the dock, then hide it', () => {
  expect(panelSizes(1280,true,true)).toMatchObject({sidebar:280,right:400,visible:true});
  expect(panelSizes(950,true,true)).toMatchObject({sidebar:64,right:400,visible:true});
  expect(panelSizes(720,true,true)).toMatchObject({sidebar:56,right:336,visible:true});
  expect(panelSizes(390,true,true)).toMatchObject({sidebar:56,right:0,visible:false});
});
test('remembered widths return when space is available and manual hiding is respected', () => {
  expect(panelSizes(1400,true,true,340,500)).toMatchObject({sidebar:340,right:500,visible:true});
  expect(panelSizes(1400,false,false,340,500)).toMatchObject({sidebar:64,right:0,visible:false});
});
