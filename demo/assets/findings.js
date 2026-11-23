const pumpFindings = [
  {
    "category": "Material",
    "label": "Material binding",
    "path": "/Pump/Housing",
    "stages": [
      {
        "value": "/Pump/Looks/BluePaint",
        "summary": "Strongest layer supplies an explicit target list.",
        "opinions": [
          {
            "layer": "pump_base.usda",
            "spec": "/Pump/Housing.material:binding",
            "value": "SdfPathListOp(Explicit Items: [/Pump/Looks/BluePaint])"
          }
        ]
      },
      {
        "value": "/Pump/Looks/Copper",
        "summary": "Strongest layer supplies an explicit target list.",
        "opinions": [
          {
            "layer": "look_v013.usda",
            "spec": "/Pump/Housing.material:binding",
            "value": "SdfPathListOp(Explicit Items: [/Pump/Looks/Copper])"
          },
          {
            "layer": "pump_base.usda",
            "spec": "/Pump/Housing.material:binding",
            "value": "SdfPathListOp(Explicit Items: [/Pump/Looks/BluePaint])"
          }
        ]
      }
    ]
  },
  {
    "category": "Transform",
    "label": "Transform \u00b7 translate",
    "path": "/Pump/Bracket",
    "stages": [
      {
        "value": "double3: (0, 0, 0)",
        "summary": "Winning default first \u00b7 weaker authored defaults follow",
        "opinions": [
          {
            "layer": "pump_base.usda",
            "spec": "/Pump/Bracket.xformOp:translate",
            "value": "(0, 0, 0)"
          }
        ]
      },
      {
        "value": "double3: (0.38, 0, 0)",
        "summary": "Winning default first \u00b7 weaker authored defaults follow",
        "opinions": [
          {
            "layer": "assembly_v013.usda",
            "spec": "/Pump/Bracket.xformOp:translate",
            "value": "(0.38, 0, 0)"
          },
          {
            "layer": "pump_base.usda",
            "spec": "/Pump/Bracket.xformOp:translate",
            "value": "(0, 0, 0)"
          }
        ]
      }
    ]
  }
];
